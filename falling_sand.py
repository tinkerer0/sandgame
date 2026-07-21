#!/usr/bin/env python3
"""터미널 폴링샌드(falling-sand) 물리 샌드박스 게임.

curses 기반 풀스크린 TUI. octant 블록(2x4) 기법으로 문자 셀 하나에
가로 2배 x 세로 4배 해상도를 담아, 모래/물/돌/불/기름/증기/식물/벌레 8종
원소가 상호작용하는 셀 오토마타를 그린다. 배경은 항상 순검정으로 강제한다.

시뮬레이션 상태(SandSim)는 curses에 전혀 의존하지 않아 headless self-test로
`--selftest` 옵션을 주면 물리 로직만 순수하게 검증할 수 있다.
표준 라이브러리만 사용하며 유일한 비자명 import는 curses다.
"""

import sys
import time
import random

# curses는 표준 라이브러리이지만 --help/--selftest 경로에서는 굳이 화면을
# 초기화하지 않는다(스펙 요구: selftest는 curses 초기화 없이 순수 시뮬만 돌림).
import curses


# ============================================================
# 원소 정의
# ============================================================

EMPTY = 0
SAND = 1
WATER = 2
STONE = 3
FIRE = 4
OIL = 5
STEAM = 6
PLANT = 7
BUG = 8

# 숫자키 1~7 순서와 정확히 일치(스펙 지정 순서). 벌레(BUG)는 숫자키에 넣지
# 않고 `/` 팔레트로만 고른다 - 기존 단축키 근육기억을 안 건드리기 위함.
ELEMENT_KEY_ORDER = [SAND, WATER, STONE, FIRE, OIL, STEAM, PLANT]

# `/` 원소 팔레트에 표시될 순서: 숫자키 순서 뒤에 벌레, 마지막에 지우개.
PALETTE_ORDER = ELEMENT_KEY_ORDER + [BUG, EMPTY]

ELEMENT_NAMES = {
    EMPTY: "지우개",
    SAND: "모래",
    WATER: "물",
    STONE: "돌",
    FIRE: "불",
    OIL: "기름",
    STEAM: "증기",
    PLANT: "식물",
    BUG: "벌레",
}

# ============================================================
# 물리 튜닝 상수
# ============================================================

FIRE_LIFE_MIN, FIRE_LIFE_MAX = 8, 20          # 불이 타는 프레임 수
STEAM_LIFE_MIN, STEAM_LIFE_MAX = 15, 35       # 증기가 유지되는 프레임 수
FIRE_QUENCH_PENALTY = 4                       # 물을 만난 불의 수명 급감량
FIRE_TO_STEAM_CHANCE = 0.12                   # 불이 꺼질 때 증기로 남을 확률
FIRE_RISE_CHANCE = 0.35                       # 불이 한 칸 위로 일렁일 확률
STEAM_CONDENSE_CHANCE = 0.15                  # 증기가 소멸/응결 굴림에서 물이 될 확률
PLANT_GROWTH_CHANCE = 0.04                    # 물 인접 식물이 성장할 확률(물을 흡수해 한 픽셀씩 천천히 자람)

# --- BUG(벌레) 튜닝 상수 --- life[y][x]를 벌레의 energy로 쓴다.
# 밸런스 2차 조정: 초기 대비 너무 빨리 먹고 너무 일찍 죽는다는 피드백 반영 -
# 시작 에너지/대사 간격/번식 문턱/상한을 올리고, 섭식 자체도 간격을 둬 늦춘다.
BUG_START_ENERGY = 140               # paint_disc로 새로 칠했을 때 초기 energy
BUG_MAX_ENERGY = 260                 # energy 상한(무한 성장 방지)
BUG_EAT_GAIN = 40                    # 식물 한 칸을 먹었을 때 얻는 energy
BUG_EAT_INTERVAL = 3                 # 인접 식물 섭식을 시도하는 tick 간격(매 프레임 X)
BUG_METABOLISM = 1                   # 대사로 잃는 energy 양
BUG_METABOLISM_INTERVAL = 8          # 대사가 실제로 적용되는 tick 간격("-1/8tick")
BUG_BREED_THRESHOLD = 200            # 이 이상이면 번식 시도
BUG_BREED_MAX_NEIGHBORS = 3          # 주변 벌레가 이 수 미만이어야 번식(밀도 제한)
BUG_MOVE_INTERVAL = 3                # 배회/추적 이동을 시도하는 tick 간격(낙하는 매 프레임)
BUG_SIGHT_RADIUS = 5                 # 먹이(식물)를 탐지하는 반경
BUG_FIRE_AVOID_RADIUS = 2            # 이동 목표 근처 이 반경 안에 불이 있으면 회피
BUG_WANDER_KEEP_CHANCE = 0.7         # 배회 시 이전 방향을 유지할 확률
BUG_DROWN_WATER_NEIGHBORS = 3        # 8이웃 중 물이 이 수 이상이면 익사 판정
BUG_DROWN_DAMAGE = 30                # 익사 판정 시 tick당 energy 피해

# octant 렌더링: 가로 sim 해상도 2배, 세로 4배. 브러시 반경을 소폭 상향한다
# (완전 비례시키면 브러시가 과하게 커지므로 적당히만).
BRUSH_RADIUS_MIN, BRUSH_RADIUS_MAX = 0, 12
# 커서 이동은 축별로 "문자 셀 1칸"에 정확히 맞춘다(octant 1 char 셀 = sim
# 가로 2px x 세로 4px) - 예전처럼 하나의 MOVE_STEP을 양쪽에 같이 쓰면 셀
# 경계를 건너뛰어 그 사이 셀이 안 지워지는 틈이 생겼다.
MOVE_STEP_X = 2                                # 가로 이동(1 char 열 = sim 2px)
MOVE_STEP_Y = 4                                # 세로 이동(1 char 행 = sim 4px)


class SandSim:
    """폴링샌드 물리 시뮬레이션 상태. curses에 의존하지 않는 순수 로직.

    grid[y][x] 형태의 2차원 리스트(row-major)로 원소를 저장하고,
    FIRE/STEAM의 수명 카운터는 같은 모양의 life 배열에 별도 보관한다.
    """

    def __init__(self, width, height, rng=None):
        self.width = width
        self.height = height
        self.rng = rng if rng is not None else random.Random()
        self.grid = [[EMPTY] * width for _ in range(height)]
        self.life = [[0] * width for _ in range(height)]
        # 한 프레임 안에서 이미 이동/변환된 셀을 다시 건드리지 않기 위한 마스크
        self._processed = [[False] * width for _ in range(height)]
        # BUG(벌레)의 마지막 이동 방향을 저장하는 보조 배열. (dx, dy)를
        # (dx+1)*3+(dy+1) 로 0~8에 패킹, 4 = 방향 없음((0,0)). 다른 원소에는
        # 쓰이지 않고 무시된다.
        self.bug_dir = [[4] * width for _ in range(height)]
        # step() 호출마다 1씩 증가하는 전역 tick 카운터. BUG의 이동/대사
        # 간격(BUG_MOVE_INTERVAL, BUG_METABOLISM_INTERVAL) 판정에 쓴다.
        self.tick = 0

    # --- 기본 접근자 ---------------------------------------------------

    def in_bounds(self, x, y):
        return 0 <= x < self.width and 0 <= y < self.height

    def get(self, x, y):
        """경계 밖은 STONE(벽)으로 취급해 낙하/확산 로직을 단순화한다."""
        if not self.in_bounds(x, y):
            return STONE
        return self.grid[y][x]

    def set(self, x, y, element, life=0):
        if self.in_bounds(x, y):
            self.grid[y][x] = element
            self.life[y][x] = life

    def clear(self):
        for y in range(self.height):
            row_g = self.grid[y]
            row_l = self.life[y]
            row_d = self.bug_dir[y]
            for x in range(self.width):
                row_g[x] = EMPTY
                row_l[x] = 0
                row_d[x] = 4

    def _paint_one(self, x, y, element):
        """paint_disc/paint_cell이 공유하는 셀 1칸 칠하기 로직.

        FIRE/STEAM/BUG는 적절한 life/energy를 새로 부여하고, 그 외 원소는
        life 0으로 칠한다(지우개 EMPTY 포함).
        """
        if element == FIRE:
            self.set(x, y, FIRE, self.rng.randint(FIRE_LIFE_MIN, FIRE_LIFE_MAX))
        elif element == STEAM:
            self.set(x, y, STEAM, self.rng.randint(STEAM_LIFE_MIN, STEAM_LIFE_MAX))
        elif element == BUG:
            self.set(x, y, BUG, BUG_START_ENERGY)
            self.bug_dir[y][x] = 4
        else:
            self.set(x, y, element, 0)
            self.bug_dir[y][x] = 4  # 위생: 죽은 벌레의 방향 잔여값이 안 남게(수정 5)

    def paint_disc(self, cx, cy, radius, element):
        """(cx, cy) 중심 반지름 radius 원반을 element로 칠한다."""
        r2 = radius * radius
        for y in range(cy - radius, cy + radius + 1):
            for x in range(cx - radius, cx + radius + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 > r2:
                    continue
                if not self.in_bounds(x, y):
                    continue
                self._paint_one(x, y, element)

    def paint_cell(self, cx, cy, element):
        """(cx, cy)가 속한 octant 문자 셀 전체(sim 2x4 = 8칸)를 element로 칠한다.

        커서는 char 셀 단위(A_REVERSE)로 강조되는데, 반지름이 작은
        paint_disc는 그 셀의 픽셀 일부만 칠/지워 나머지가 남을 수 있다.
        커서가 있는 칸은 브러시 크기와 무관하게 항상 통째로 칠/지워지도록
        paint_disc와 나란히 호출한다(반경>0이면 disc가 셀 밖까지 추가로
        넓게 칠한다).
        """
        base_x = (cx // 2) * 2
        base_y = (cy // 4) * 4
        for dy in range(4):
            for dx in range(2):
                x, y = base_x + dx, base_y + dy
                if self.in_bounds(x, y):
                    self._paint_one(x, y, element)

    # --- 이동 헬퍼 -------------------------------------------------------

    def _move(self, x, y, nx, ny):
        self.grid[ny][nx] = self.grid[y][x]
        self.life[ny][nx] = self.life[y][x]
        self.bug_dir[ny][nx] = self.bug_dir[y][x]
        self.grid[y][x] = EMPTY
        self.life[y][x] = 0
        self.bug_dir[y][x] = 4
        self._processed[ny][nx] = True
        self._processed[y][x] = True

    def _swap(self, x, y, nx, ny):
        self.grid[y][x], self.grid[ny][nx] = self.grid[ny][nx], self.grid[y][x]
        self.life[y][x], self.life[ny][nx] = self.life[ny][nx], self.life[y][x]
        self.bug_dir[y][x], self.bug_dir[ny][nx] = self.bug_dir[ny][nx], self.bug_dir[y][x]
        self._processed[y][x] = True
        self._processed[ny][nx] = True

    # --- 원소별 물리 규칙 -------------------------------------------------

    def _step_sand(self, x, y):
        """모래: 아래로 낙하, 막히면 대각선. 물/기름보다 무거워 가라앉는다."""
        below = self.get(x, y + 1)
        if below == EMPTY:
            self._move(x, y, x, y + 1)
            return
        if below in (WATER, OIL):
            self._swap(x, y, x, y + 1)
            return
        order = [-1, 1]
        self.rng.shuffle(order)
        for dx in order:
            nx = x + dx
            cell = self.get(nx, y + 1)
            if cell == EMPTY:
                self._move(x, y, nx, y + 1)
                return
            if cell in (WATER, OIL):
                self._swap(x, y, nx, y + 1)
                return
        self._processed[y][x] = True

    def _step_water(self, x, y):
        """물: 아래->대각선->좌우 확산. 기름보다 무거워 기름을 위로 띄운다."""
        below = self.get(x, y + 1)
        if below == EMPTY:
            self._move(x, y, x, y + 1)
            return
        if below == OIL:
            self._swap(x, y, x, y + 1)
            return
        order = [-1, 1]
        self.rng.shuffle(order)
        for dx in order:
            nx = x + dx
            cell = self.get(nx, y + 1)
            if cell == EMPTY:
                self._move(x, y, nx, y + 1)
                return
            if cell == OIL:
                self._swap(x, y, nx, y + 1)
                return
        order2 = [-1, 1]
        self.rng.shuffle(order2)
        for dx in order2:
            nx = x + dx
            if self.get(nx, y) == EMPTY:
                self._move(x, y, nx, y)
                return
        self._processed[y][x] = True

    def _step_oil(self, x, y):
        """기름: 물처럼 흐르되 가벼워 물을 만나면 위로 뜬다. 인화성.

        아래가 WATER일 때의 direct down-swap은 일부러 없앴다(A1) - 부양은
        _step_water가 "아래가 OIL이면 swap"으로 이미 처리하므로, 기름 쪽에도
        대칭 규칙이 있으면 안정적으로 성층된 oil-위/water-아래 경계가
        매 프레임 위아래로 뒤집히는 오실레이션이 생겼다.

        대각선 쪽의 WATER-swap도 같은 이유로 제거했다(수정 2) - 경사면이나
        구석에서 OIL의 대각선 swap과 WATER의 대각선 swap(둘 다 대칭 규칙)이
        주기 2로 무한 진동하는 게 실측 확인됐다. WATER 쪽 대각선 swap만
        남겨 비대칭으로 고정한다. 낙하(EMPTY로 내려가기)·좌우 흐름은 그대로.
        """
        below = self.get(x, y + 1)
        if below == EMPTY:
            self._move(x, y, x, y + 1)
            return
        order = [-1, 1]
        self.rng.shuffle(order)
        for dx in order:
            nx = x + dx
            cell = self.get(nx, y + 1)
            if cell == EMPTY:
                self._move(x, y, nx, y + 1)
                return
        order2 = [-1, 1]
        self.rng.shuffle(order2)
        for dx in order2:
            nx = x + dx
            if self.get(nx, y) == EMPTY:
                self._move(x, y, nx, y)
                return
        self._processed[y][x] = True

    def _step_fire(self, x, y):
        """불: 인접 인화성 물체 점화, 물은 tick당 1칸만 증기로, 수명 소진 시 소멸/증기화."""
        neighbors8 = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
        order = list(neighbors8)
        self.rng.shuffle(order)  # 특정 방향의 물이 항상 먼저 꺼지는 편향 방지
        quenched = False
        water_quenched_this_tick = False  # A2: 물은 tick당 최대 1칸만 증기화(소방선 조절 가능해짐)
        for dx, dy in order:
            nx, ny = x + dx, y + dy
            if not self.in_bounds(nx, ny):
                continue
            # 주의: 여기서 processed 여부는 보지 않는다 - "이번 프레임에 이미
            # 낙하 로직을 거쳤다"는 것과 "점화 가능한가"는 별개 개념이다.
            # ncell 값 자체가 최신 상태이므로 이미 FIRE/STEAM으로 바뀐
            # 이웃은 아래 분기 어디에도 걸리지 않아 자연히 안전하다.
            ncell = self.grid[ny][nx]
            if ncell == OIL or ncell == PLANT or ncell == BUG:
                self.grid[ny][nx] = FIRE
                self.life[ny][nx] = self.rng.randint(FIRE_LIFE_MIN, FIRE_LIFE_MAX)
                self.bug_dir[ny][nx] = 4  # 위생: BUG가 타 죽은 경우 방향 잔여값 정리(수정 5)
                self._processed[ny][nx] = True
            elif ncell == WATER and not water_quenched_this_tick:
                self.grid[ny][nx] = STEAM
                self.life[ny][nx] = self.rng.randint(STEAM_LIFE_MIN, STEAM_LIFE_MAX)
                self._processed[ny][nx] = True
                quenched = True
                water_quenched_this_tick = True

        life = self.life[y][x] - 1
        if quenched:
            life -= FIRE_QUENCH_PENALTY

        if life <= 0:
            if self.rng.random() < FIRE_TO_STEAM_CHANCE:
                self.grid[y][x] = STEAM
                self.life[y][x] = self.rng.randint(STEAM_LIFE_MIN, STEAM_LIFE_MAX)
            else:
                self.grid[y][x] = EMPTY
                self.life[y][x] = 0
            self._processed[y][x] = True
            return

        if self.rng.random() < FIRE_RISE_CHANCE:
            order = [-1, 1]
            self.rng.shuffle(order)
            rise_candidates = [(x, y - 1), (x + order[0], y - 1), (x + order[1], y - 1)]
            for nx, ny in rise_candidates:
                if (self.in_bounds(nx, ny) and not self._processed[ny][nx]
                        and self.grid[ny][nx] in (EMPTY, STEAM)):
                    target_el = self.grid[ny][nx]
                    target_life = self.life[ny][nx]
                    self.grid[ny][nx] = FIRE
                    self.life[ny][nx] = life
                    self.grid[y][x] = target_el
                    self.life[y][x] = target_life
                    self._processed[ny][nx] = True
                    self._processed[y][x] = True
                    return

        self.life[y][x] = life
        self._processed[y][x] = True

    def _step_steam(self, x, y):
        """증기: 위로 상승. 막히거나(A3) STONE/PLANT 접촉("차가운 표면") 시,
        혹은 수명 소진·꼭대기 도달 시 STEAM_CONDENSE_CHANCE로 물 응결을
        굴려본다 - 실패하면 수명 소진/꼭대기 케이스만 소멸하고, 막힘/접촉
        케이스는 증기로 남아 다음 프레임에 다시 시도한다("천장 비" 순환).
        """
        life = self.life[y][x] - 1
        fx, fy = x, y
        order = [-1, 1]
        self.rng.shuffle(order)
        candidates = [(x, y - 1), (x + order[0], y - 1), (x + order[1], y - 1)]
        moved_up = False
        for nx, ny in candidates:
            if self.in_bounds(nx, ny) and not self._processed[ny][nx] and self.grid[ny][nx] == EMPTY:
                fx, fy = nx, ny
                moved_up = True
                break

        if moved_up:
            self.grid[y][x] = EMPTY
            self.life[y][x] = 0

        reached_top = (fy == 0)
        blocked = not moved_up
        touches_cold_surface = any(
            self.in_bounds(fx + dx, fy + dy) and self.grid[fy + dy][fx + dx] in (STONE, PLANT)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)
        )

        if (life <= 0 or reached_top or blocked or touches_cold_surface) and self.rng.random() < STEAM_CONDENSE_CHANCE:
            self.grid[fy][fx] = WATER
            self.life[fy][fx] = 0
        elif life <= 0 or reached_top:
            self.grid[fy][fx] = EMPTY
            self.life[fy][fx] = 0
        else:
            self.grid[fy][fx] = STEAM
            self.life[fy][fx] = life
        self._processed[fy][fx] = True

    def _step_plant(self, x, y):
        """식물: 낙하하지 않는 고정 원소. 물 인접 시 위 방향 편향으로 성장.

        A4(물질 보존): WATER 칸으로 자라면 그 칸 자체가 소비되니 그걸로
        끝. EMPTY 칸으로 자랄 때는 그냥 공짜로 자라지 않도록, 인접 물 중
        한 칸을 "마셔서"(EMPTY로) 실제로 소비한다.
        """
        neighbors4 = [(0, -1), (0, 1), (-1, 0), (1, 0)]
        water_spots = [
            (x + dx, y + dy) for dx, dy in neighbors4
            if self.in_bounds(x + dx, y + dy) and self.grid[y + dy][x + dx] == WATER
        ]
        if water_spots and self.rng.random() < PLANT_GROWTH_CHANCE:
            candidates = [(x, y - 1), (x - 1, y - 1), (x + 1, y - 1),
                          (x - 1, y), (x + 1, y), (x, y + 1)]
            self.rng.shuffle(candidates)
            candidates.sort(key=lambda p: p[1])  # 위쪽(y가 작은 쪽) 우선
            for nx, ny in candidates:
                # ny > y(아래 행)는 이번 프레임에 이미 처리가 끝난 행이라
                # processed 여부와 무관하게 항상 허용한다(수정 3) - 아래→위
                # 순회라 아래 행을 "아직 처리 안 됐다"고 막을 이유가 없다.
                # 같은 행/위 행은 기존대로 processed 체크로 중복 방지.
                if (self.in_bounds(nx, ny) and (ny > y or not self._processed[ny][nx])
                        and self.grid[ny][nx] in (EMPTY, WATER)):
                    grew_into_water = (self.grid[ny][nx] == WATER)
                    self.grid[ny][nx] = PLANT
                    self.life[ny][nx] = 0
                    self._processed[ny][nx] = True
                    if not grew_into_water:
                        shuffled_water = list(water_spots)
                        self.rng.shuffle(shuffled_water)
                        for wx, wy in shuffled_water:
                            if not self._processed[wy][wx] and self.grid[wy][wx] == WATER:
                                self.grid[wy][wx] = EMPTY
                                self.life[wy][wx] = 0
                                self._processed[wy][wx] = True
                                break
                    break
        self._processed[y][x] = True

    # --- BUG(벌레) 헬퍼 ---------------------------------------------------

    def _get_dir(self, x, y):
        """bug_dir 패킹값을 (dx, dy)로 푼다(_set_dir의 정확한 역연산).

        _set_dir이 `code = (dx+1)*3 + (dy+1)`로 패킹하므로 역연산은
        `dx = code//3 - 1`, `dy = code%3 - 1`이다 - 이전엔 이 둘이 뒤바뀌어
        있어(버그) 벌레의 저장된 이동 방향이 실제와 반대축으로 읽혔다.
        """
        code = self.bug_dir[y][x]
        return (code // 3) - 1, (code % 3) - 1

    def _set_dir(self, x, y, dx, dy):
        """(dx, dy)를 bug_dir에 패킹해 저장한다."""
        self.bug_dir[y][x] = (dx + 1) * 3 + (dy + 1)

    def _is_supported_empty(self, x, y):
        """빈 칸이고 바로 아래에 뭔가 있어(경계 포함) 벌레가 디딜 수 있는가."""
        if not self.in_bounds(x, y) or self.grid[y][x] != EMPTY:
            return False
        return self.get(x, y + 1) != EMPTY

    def _near_fire(self, x, y, radius):
        """(x, y) 주변 radius 안에 FIRE가 있는지."""
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                nx, ny = x + dx, y + dy
                if self.in_bounds(nx, ny) and self.grid[ny][nx] == FIRE:
                    return True
        return False

    def _find_nearest_plant(self, x, y, radius):
        """반경 안에서 가장 가까운 PLANT 좌표(없으면 None)."""
        best = None
        best_d2 = radius * radius + 1
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if self.in_bounds(nx, ny) and self.grid[ny][nx] == PLANT:
                    d2 = dx * dx + dy * dy
                    if d2 < best_d2:
                        best_d2 = d2
                        best = (nx, ny)
        return best

    def _choose_move_target(self, x, y):
        """벌레의 이번 tick 이동 목표 (nx, ny, dx, dy)를 고른다(없으면 None).

        가까운 식물 쪽 방향을 우선 시도하고(1칸짜리 장애물은 위로 올라
        넘는 후보를 추가), 없거나 막히면 이전 방향을 ~70% 확률로 유지하며
        배회한다. 어느 경우든 반경 BUG_FIRE_AVOID_RADIUS 안에 불이 있는
        칸은 강하게 회피(후보에서 제외)한다.
        """
        candidates = []
        target = self._find_nearest_plant(x, y, BUG_SIGHT_RADIUS)
        if target is not None:
            tx, ty = target
            ddx = (tx > x) - (tx < x)
            ddy = (ty > y) - (ty < y)
            for cdx, cdy in ((ddx, ddy), (ddx, 0), (0, ddy)):
                if (cdx, cdy) != (0, 0) and (cdx, cdy) not in candidates:
                    candidates.append((cdx, cdy))
            if candidates:
                cdx, cdy = candidates[0]
                # 한 칸 높이 장애물(가로로 막힘)은 위로 올라 넘는 후보를 추가
                if cdx != 0 and not self._is_supported_empty(x + cdx, y + cdy):
                    climb = (cdx, -1)
                    if climb not in candidates:
                        candidates.append(climb)

        prev_dx, prev_dy = self._get_dir(x, y)
        if (prev_dx, prev_dy) != (0, 0) and self.rng.random() < BUG_WANDER_KEEP_CHANCE:
            candidates.append((prev_dx, prev_dy))
        random_dirs = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
        self.rng.shuffle(random_dirs)
        candidates.extend(random_dirs)

        for cdx, cdy in candidates:
            nx, ny = x + cdx, y + cdy
            # ny > y(아래 행)는 이미 처리 끝난 행이라 processed와 무관하게
            # 항상 허용(수정 3) - _is_supported_empty의 grid==EMPTY 체크가
            # 중복 이동을 막아준다.
            if (self._is_supported_empty(nx, ny) and (ny > y or not self._processed[ny][nx])
                    and not self._near_fire(nx, ny, BUG_FIRE_AVOID_RADIUS)):
                return nx, ny, cdx, cdy
        return None

    def _step_bug(self, x, y):
        """벌레: 식물을 먹고 자라며 번식하는 간단한 생물 원소.

        순서 - 대사(항상 판정, METABOLISM_INTERVAL마다 실제 차감) -> 침수
        판정 -> 인접 식물 먹기(EAT_INTERVAL마다) -> 중력 낙하 -> (MOVE_INTERVAL
        마다) 먹이 방향 이동/배회 -> 번식. 불에 타는 것 자체는 _step_fire의
        인화 대상 목록이 처리하므로(BUG도 OIL/PLANT처럼 점화 대상), 여기서는
        화염 회피(이동 후보 필터)만 담당한다.
        """
        energy = self.life[y][x]

        # 대사: 항상 굶주려 가지만, 실제 차감은 간격을 둬 매 프레임 조금씩만.
        if self.tick % BUG_METABOLISM_INTERVAL == 0:
            energy -= BUG_METABOLISM

        # 침수 판정: 8이웃에 물이 BUG_DROWN_WATER_NEIGHBORS칸 이상이면 익사 피해.
        water_neighbors = sum(
            1 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
            if (dx, dy) != (0, 0) and self.get(x + dx, y + dy) == WATER
        )
        if water_neighbors >= BUG_DROWN_WATER_NEIGHBORS:
            energy -= BUG_DROWN_DAMAGE

        if energy <= 0:
            self.grid[y][x] = EMPTY
            self.life[y][x] = 0
            self._set_dir(x, y, 0, 0)
            self._processed[y][x] = True
            return
        self.life[y][x] = energy

        # 인접 식물 먹기 - 매 프레임이 아니라 BUG_EAT_INTERVAL마다만 시도한다
        # (너무 빨리 먹어치운다는 피드백 반영). 게이트 밖이면 시도조차 하지
        # 않고 바로 다음 단계(낙하/이동)로 넘어간다.
        if (self.tick + x + y) % BUG_EAT_INTERVAL == 0:
            eat_dirs = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]
            self.rng.shuffle(eat_dirs)
            for dx, dy in eat_dirs:
                nx, ny = x + dx, y + dy
                # ny > y(아래 행)는 이미 처리 끝난 행이라 processed와 무관하게
                # 항상 허용(수정 3) - grid==PLANT 체크가 중복소비를 막아준다.
                if (self.in_bounds(nx, ny) and self.grid[ny][nx] == PLANT
                        and (ny > y or not self._processed[ny][nx])):
                    self.life[y][x] = min(BUG_MAX_ENERGY, self.life[y][x] + BUG_EAT_GAIN)
                    self._move(x, y, nx, ny)
                    return

        # 중력 낙하(모래 등과 동일하게 매 프레임) - 단, 인접 8칸에 식물이
        # 있으면 매달려(climb) 안 떨어진다. 안 그러면 EAT_INTERVAL 게이트로
        # 못 먹는 틱마다 매번 떨어져서, 식물을 타고 위로 못 올라간다(식물을
        # 다 먹으면 매달릴 게 없어져 자연히 떨어짐).
        below = self.get(x, y + 1)
        near_plant = any(
            self.get(x + dx, y + dy) == PLANT
            for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)
        )
        if below == EMPTY and not near_plant:
            self._move(x, y, x, y + 1)
            return

        # 먹이 방향 이동/배회 - move-interval마다만 시도.
        if (self.tick + x + y) % BUG_MOVE_INTERVAL == 0:
            target = self._choose_move_target(x, y)
            if target is not None:
                nx, ny, dx, dy = target
                self._set_dir(x, y, dx, dy)
                self._move(x, y, nx, ny)
                return

        # 번식: 충분히 배부르고 주변이 너무 붐비지 않으면.
        if self.life[y][x] >= BUG_BREED_THRESHOLD:
            neighbor_bugs = sum(
                1 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                if (dx, dy) != (0, 0) and self.get(x + dx, y + dy) == BUG
            )
            if neighbor_bugs < BUG_BREED_MAX_NEIGHBORS:
                spots = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]
                self.rng.shuffle(spots)
                for dx, dy in spots:
                    nx, ny = x + dx, y + dy
                    # ny > y(아래 행)는 이미 처리 끝난 행이라 processed와 무관
                    # 하게 항상 허용(수정 3).
                    if self._is_supported_empty(nx, ny) and (ny > y or not self._processed[ny][nx]):
                        child_energy = self.life[y][x] // 2
                        self.life[y][x] -= child_energy
                        self.grid[ny][nx] = BUG
                        self.life[ny][nx] = child_energy
                        self._set_dir(nx, ny, 0, 0)
                        self._processed[ny][nx] = True
                        break

        self._processed[y][x] = True

    # --- 메인 스텝 --------------------------------------------------------

    def step(self):
        """물리 한 프레임을 진행한다. 아래->위 행 순회로 이중이동을 막는다."""
        self.tick += 1
        for row in self._processed:
            for i in range(len(row)):
                row[i] = False

        for y in range(self.height - 1, -1, -1):
            xs = list(range(self.width))
            self.rng.shuffle(xs)
            for x in xs:
                if self._processed[y][x]:
                    continue
                el = self.grid[y][x]
                if el == EMPTY or el == STONE:
                    continue
                elif el == SAND:
                    self._step_sand(x, y)
                elif el == WATER:
                    self._step_water(x, y)
                elif el == OIL:
                    self._step_oil(x, y)
                elif el == FIRE:
                    self._step_fire(x, y)
                elif el == BUG:
                    self._step_bug(x, y)
                elif el == STEAM:
                    self._step_steam(x, y)
                elif el == PLANT:
                    self._step_plant(x, y)


# ============================================================
# 색상 (256색 매핑)
# ============================================================

FIRE_FLICKER = [196, 202, 208, 214, 220, 226]   # 빨강 -> 주황 -> 노랑
WATER_SHADES = [17, 18, 19, 24, 25, 26]         # 파랑 계열
SAND_SHADES = [136, 130, 94, 172]               # 탄/카키 계열
STONE_SHADES = [240, 244, 248, 252]             # 회색 계열
OIL_SHADES = [58, 100, 3, 64]                   # 어두운 갈색/올리브
STEAM_SHADES = [255, 254, 253, 250, 15, 7]      # 밝은 회색/흰색
PLANT_SHADES = [22, 28, 34, 40, 46]             # 초록 계열
BUG_SHADES = [201, 165, 207]                    # 마젠타/분홍 계열 - 식물 초록과 뚜렷이 대비

UI_FG = 15                                      # HUD/팔레트 텍스트용 밝은 흰색(검정 배경 위 가독성)


def element_color(element, render_rng):
    """원소를 256색 인덱스로 매핑한다. 불 등은 프레임마다 일렁이도록 랜덤 선택."""
    if element == EMPTY:
        return -1  # 터미널 기본 배경색
    if element == SAND:
        return render_rng.choice(SAND_SHADES)
    if element == WATER:
        return render_rng.choice(WATER_SHADES)
    if element == STONE:
        return render_rng.choice(STONE_SHADES)
    if element == FIRE:
        return render_rng.choice(FIRE_FLICKER)
    if element == OIL:
        return render_rng.choice(OIL_SHADES)
    if element == STEAM:
        return render_rng.choice(STEAM_SHADES)
    if element == PLANT:
        return render_rng.choice(PLANT_SHADES)
    if element == BUG:
        return render_rng.choice(BUG_SHADES)
    return -1


class ColorPairCache:
    """(fg, BLACK) 조합마다 curses color pair를 on-demand 생성해 캐시한다.

    배경은 항상 순검정으로 강제한다 - 호출부가 넘기는 bg 인자는 무시하고
    내부적으로 self.black_bg로 대체한다(터미널/Orca 테마 배경이 비치는 것을
    막기 위함). 256색 미만 터미널에서는 색 인덱스를 사용 가능 범위로 눌러
    담아 graceful degrade하고, color pair 예산을 넘기면 기본 페어(0)로 대체한다.
    """

    def __init__(self):
        self.cache = {}
        self.next_id = 1
        has_color = curses.has_colors()
        self.max_colors = max(curses.COLORS, 1) if has_color else 1
        self.max_pairs = max(curses.COLOR_PAIRS, 1) if has_color else 1
        # 순검정: 256색 터미널은 인덱스 16(#000000), 아니면 표준 COLOR_BLACK.
        self.black_bg = 16 if (has_color and curses.COLORS >= 256) else curses.COLOR_BLACK

    def _clamp(self, idx):
        if idx < 0:
            return -1
        if idx >= self.max_colors:
            return idx % self.max_colors
        return idx

    def get_pair(self, fg, bg=None):
        """bg 인자는 무시하고 배경은 항상 self.black_bg로 강제한다."""
        fg = self._clamp(fg)
        bg_val = self._clamp(self.black_bg)
        key = (fg, bg_val)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.next_id >= self.max_pairs:
            return 0
        pair_id = self.next_id
        try:
            curses.init_pair(pair_id, fg, bg_val)
        except curses.error:
            return 0
        self.cache[key] = pair_id
        self.next_id += 1
        return pair_id

    def black_on_black_pair(self):
        """화면 전체 배경(bkgd)을 순검정으로 채우기 위한 (BLACK, BLACK) 페어."""
        return self.get_pair(self.black_bg)


# ============================================================
# octant 렌더링 - 문자 셀 1개 = sim 픽셀 2x4 (가로x세로)
# ============================================================

# octant 비트 배치(2열 x 4행): bit0=(col0,row0)=1, bit1=(col1,row0)=2,
# bit2=(col0,row1)=4, bit3=(col1,row1)=8, bit4=(col0,row2)=16,
# bit5=(col1,row2)=32, bit6=(col0,row3)=64, bit7=(col1,row3)=128.
# v(0~255) = 켜진 비트의 합 -> 256개 코드포인트 룩업.
#
# 출처: Unicode 16.0(2024) "Symbols for Legacy Computing Supplement" 공식
# 데이터를 두 독립 경로로 직접 대조해 만들었다 - (1) unicode.org의
# NamesList.txt를 내려받아 "BLOCK OCTANT-N" 230개 항목을 파싱, (2) 같은
# unicode.org의 공식 코드차트 PDF(U160-1CC00.pdf)의 이름 목록으로 재확인.
# 두 출처가 정확히 일치했다. 230개는 U+1CD00~U+1CDE5(옥탄트 전용 문자),
# 나머지 26개는 이미 존재하는 문자를 재사용하는 자리라 아래처럼 처리:
#   - 22개는 명칭으로 도형이 정확히 특정되는 기존 문자로 확정
#     (공백/꽉참/상하좌우 반칸·1/4·3/4 블록, QUADRANT 8종 조합,
#     U+1FBE6/1FBE7 MIDDLE LEFT/RIGHT ONE QUARTER BLOCK).
#   - 4개(모서리 8분의 1칸 단독 - v=1,2,64,128)는 1FB00~1FBFF·2580~259F·
#     1CC00~1CEBF 전 범위를 뒤졌지만 authoritative 대응 문자를 못 찾았다.
#     그 칸을 포함하는 QUADRANT 문자로 근사(실제보다 넓게 보임 - 완전
#     확정 매핑 아님, 아래 주석에 명시).
OCTANT_CODEPOINTS = [
    0x20, 0x2598, 0x259D, 0x1FB82, 0x1CD00, 0x2598, 0x1CD01, 0x1CD02,
    0x1CD03, 0x1CD04, 0x259D, 0x1CD05, 0x1CD06, 0x1CD07, 0x1CD08, 0x2580,
    0x1CD09, 0x1CD0A, 0x1CD0B, 0x1CD0C, 0x1FBE6, 0x1CD0D, 0x1CD0E, 0x1CD0F,
    0x1CD10, 0x1CD11, 0x1CD12, 0x1CD13, 0x1CD14, 0x1CD15, 0x1CD16, 0x1CD17,
    0x1CD18, 0x1CD19, 0x1CD1A, 0x1CD1B, 0x1CD1C, 0x1CD1D, 0x1CD1E, 0x1CD1F,
    0x1FBE7, 0x1CD20, 0x1CD21, 0x1CD22, 0x1CD23, 0x1CD24, 0x1CD25, 0x1CD26,
    0x1CD27, 0x1CD28, 0x1CD29, 0x1CD2A, 0x1CD2B, 0x1CD2C, 0x1CD2D, 0x1CD2E,
    0x1CD2F, 0x1CD30, 0x1CD31, 0x1CD32, 0x1CD33, 0x1CD34, 0x1CD35, 0x1FB85,
    0x2596, 0x1CD36, 0x1CD37, 0x1CD38, 0x1CD39, 0x1CD3A, 0x1CD3B, 0x1CD3C,
    0x1CD3D, 0x1CD3E, 0x1CD3F, 0x1CD40, 0x1CD41, 0x1CD42, 0x1CD43, 0x1CD44,
    0x2596, 0x1CD45, 0x1CD46, 0x1CD47, 0x1CD48, 0x258C, 0x1CD49, 0x1CD4A,
    0x1CD4B, 0x1CD4C, 0x259E, 0x1CD4D, 0x1CD4E, 0x1CD4F, 0x1CD50, 0x259B,
    0x1CD51, 0x1CD52, 0x1CD53, 0x1CD54, 0x1CD55, 0x1CD56, 0x1CD57, 0x1CD58,
    0x1CD59, 0x1CD5A, 0x1CD5B, 0x1CD5C, 0x1CD5D, 0x1CD5E, 0x1CD5F, 0x1CD60,
    0x1CD61, 0x1CD62, 0x1CD63, 0x1CD64, 0x1CD65, 0x1CD66, 0x1CD67, 0x1CD68,
    0x1CD69, 0x1CD6A, 0x1CD6B, 0x1CD6C, 0x1CD6D, 0x1CD6E, 0x1CD6F, 0x1CD70,
    0x2597, 0x1CD71, 0x1CD72, 0x1CD73, 0x1CD74, 0x1CD75, 0x1CD76, 0x1CD77,
    0x1CD78, 0x1CD79, 0x1CD7A, 0x1CD7B, 0x1CD7C, 0x1CD7D, 0x1CD7E, 0x1CD7F,
    0x1CD80, 0x1CD81, 0x1CD82, 0x1CD83, 0x1CD84, 0x1CD85, 0x1CD86, 0x1CD87,
    0x1CD88, 0x1CD89, 0x1CD8A, 0x1CD8B, 0x1CD8C, 0x1CD8D, 0x1CD8E, 0x1CD8F,
    0x2597, 0x1CD90, 0x1CD91, 0x1CD92, 0x1CD93, 0x259A, 0x1CD94, 0x1CD95,
    0x1CD96, 0x1CD97, 0x2590, 0x1CD98, 0x1CD99, 0x1CD9A, 0x1CD9B, 0x259C,
    0x1CD9C, 0x1CD9D, 0x1CD9E, 0x1CD9F, 0x1CDA0, 0x1CDA1, 0x1CDA2, 0x1CDA3,
    0x1CDA4, 0x1CDA5, 0x1CDA6, 0x1CDA7, 0x1CDA8, 0x1CDA9, 0x1CDAA, 0x1CDAB,
    0x2582, 0x1CDAC, 0x1CDAD, 0x1CDAE, 0x1CDAF, 0x1CDB0, 0x1CDB1, 0x1CDB2,
    0x1CDB3, 0x1CDB4, 0x1CDB5, 0x1CDB6, 0x1CDB7, 0x1CDB8, 0x1CDB9, 0x1CDBA,
    0x1CDBB, 0x1CDBC, 0x1CDBD, 0x1CDBE, 0x1CDBF, 0x1CDC0, 0x1CDC1, 0x1CDC2,
    0x1CDC3, 0x1CDC4, 0x1CDC5, 0x1CDC6, 0x1CDC7, 0x1CDC8, 0x1CDC9, 0x1CDCA,
    0x1CDCB, 0x1CDCC, 0x1CDCD, 0x1CDCE, 0x1CDCF, 0x1CDD0, 0x1CDD1, 0x1CDD2,
    0x1CDD3, 0x1CDD4, 0x1CDD5, 0x1CDD6, 0x1CDD7, 0x1CDD8, 0x1CDD9, 0x1CDDA,
    0x2584, 0x1CDDB, 0x1CDDC, 0x1CDDD, 0x1CDDE, 0x2599, 0x1CDDF, 0x1CDE0,
    0x1CDE1, 0x1CDE2, 0x259F, 0x1CDE3, 0x2586, 0x1CDE4, 0x1CDE5, 0x2588,
]

# 0~255 전부 미리 chr()로 변환해 둔 룩업(사분면/sextant 시절과 같은 패턴).
OCTANT_GLYPHS = [chr(cp) for cp in OCTANT_CODEPOINTS]

# 렌더 fg 선택 우선순위(낮을수록 우선) - 벌레 > 불 > 액체(물/기름) > 고체·기타.
# 우선순위가 가장 높은 원소는 셀 안에 단 1칸만 있어도 fg로 뽑힌다(다수결이
# 아니다) - 식물 속 벌레 1칸, 반응 전선의 불 한 칸이 안 묻히게 하기 위함.
RENDER_PRIORITY = {
    BUG: 0,
    FIRE: 1,
    WATER: 2,
    OIL: 2,
    SAND: 3,
    STONE: 3,
    PLANT: 3,
    STEAM: 3,
}


def _cell_element(sim, x, y):
    """sim 경계 밖 서브픽셀은 EMPTY로 취급한다(get()의 STONE 반환과는 다름)."""
    if 0 <= x < sim.width and 0 <= y < sim.height:
        return sim.grid[y][x]
    return EMPTY


def render_cell(sim, row, col, render_rng):
    """문자 셀(row, col) 하나에 대응하는 2x4 sim 픽셀을 octant 글리프로 압축한다.

    배경은 어차피 ColorPairCache가 항상 검정으로 강제하므로 bg는 형식상
    -1을 반환한다(무시됨).

    색은 서브픽셀의 실제 색상이 아니라 "원소(element id)" 기준으로 묶는다 -
    FIRE처럼 element_color가 프레임마다 랜덤 셰이드를 돌려주는 원소를 색으로
    묶으면 같은 불인데도 서브픽셀마다 다른 색이 나와 글리프가 들쭉날쭉해진다.

    반환: (glyph, fg_color, bg_color). 켜진 점이 하나도 없으면 공백을 반환한다.
    """
    base_x = col * 2
    base_y = row * 4
    c0r0 = _cell_element(sim, base_x, base_y)
    c1r0 = _cell_element(sim, base_x + 1, base_y)
    c0r1 = _cell_element(sim, base_x, base_y + 1)
    c1r1 = _cell_element(sim, base_x + 1, base_y + 1)
    c0r2 = _cell_element(sim, base_x, base_y + 2)
    c1r2 = _cell_element(sim, base_x + 1, base_y + 2)
    c0r3 = _cell_element(sim, base_x, base_y + 3)
    c1r3 = _cell_element(sim, base_x + 1, base_y + 3)

    counts = {}
    for el in (c0r0, c1r0, c0r1, c1r1, c0r2, c1r2, c0r3, c1r3):
        if el != EMPTY:
            counts[el] = counts.get(el, 0) + 1

    if not counts:
        return " ", -1, -1

    # RENDER_PRIORITY가 가장 높은(값이 낮은) 원소를 전경으로. 같은 우선순위
    # 등급 안에서는 개수 많은 쪽, 그래도 동률이면 원소 id 작은 쪽으로
    # 결정적으로(deterministic) 고정한다. dither는 하지 않고 단일 색만 고른다.
    fg_el = min(counts, key=lambda el: (RENDER_PRIORITY.get(el, 9), -counts[el], el))
    fg = element_color(fg_el, render_rng)

    bits = 0
    if c0r0 != EMPTY:
        bits |= 1
    if c1r0 != EMPTY:
        bits |= 2
    if c0r1 != EMPTY:
        bits |= 4
    if c1r1 != EMPTY:
        bits |= 8
    if c0r2 != EMPTY:
        bits |= 16
    if c1r2 != EMPTY:
        bits |= 32
    if c0r3 != EMPTY:
        bits |= 64
    if c1r3 != EMPTY:
        bits |= 128

    return OCTANT_GLYPHS[bits], fg, -1


# ============================================================
# 게임 상태(입력/커서/브러시) - curses 렌더 루프 전용
# ============================================================

class GameState:
    """커서·브러시·모드 등 플레이 상태. 시뮬레이션 자체와는 분리."""

    def __init__(self, width, height):
        self.cx = width // 2
        self.cy = height // 2
        self.selected = SAND
        self.brush_radius = 4  # 브라유(가로2배x세로4배) 해상도에 맞춘 조작감 보정
        # SPACE로 켜고 끄는 "그리기" 지속 상태(토글). hold 흉내(spray_until)는
        # 방향키를 같이 누르면 OS key-repeat가 방향키로 넘어가 SPACE 반복이
        # 끊겨 분사가 멈추는 문제가 있어 폐기 - 토글이면 움직이며 계속 그려진다.
        self.drawing = False
        self.paused = False
        # `/` 팔레트: 열림 여부 + 현재 강조된 행 인덱스(PALETTE_ORDER 기준) +
        # 활성 열(0=원소, 1=브러시 크기).
        self.palette_open = False
        self.palette_index = 0
        self.palette_col = 0
        self.quit = False

    def clamp_cursor(self, width, height):
        self.cx = max(0, min(width - 1, self.cx))
        self.cy = max(0, min(height - 1, self.cy))


# 팔레트 스와치 색은 element_color처럼 프레임마다 깜빡이지 않고 원소당
# 대표색 하나로 고정한다 - 고르는 동안 목록이 어지럽게 흔들리지 않도록.
PALETTE_SWATCH_COLOR = {
    SAND: SAND_SHADES[0],
    WATER: WATER_SHADES[0],
    STONE: STONE_SHADES[0],
    FIRE: FIRE_FLICKER[2],
    OIL: OIL_SHADES[0],
    STEAM: STEAM_SHADES[0],
    PLANT: PLANT_SHADES[0],
    BUG: BUG_SHADES[0],
    EMPTY: 244,  # 회색(지우개) - element_color(EMPTY)는 -1(기본색)이라 따로 지정
}


PALETTE_RIGHT_HEADER = "브러시 크기"
PALETTE_RIGHT_HINT = "↑/↓ 로 조절"


def draw_palette(stdscr, max_y, max_x, state, pairs):
    """`/` 팔레트 오버레이: 왼쪽(원소) / 오른쪽(브러시 크기) 2단 메뉴.

    state.palette_col(0=원소, 1=브러시)로 활성 열을 표시한다 - 활성 열의
    제목은 밝게(BOLD), 비활성 열은 흐리게(A_DIM) 그리고, 활성 열 "안"의
    강조 항목만 A_REVERSE를 쓴다(비활성 열엔 reverse를 쓰지 않는다).
    검정 배경 위라 테두리/채우기 없이 텍스트만 그린다. 물리는 이 함수와
    무관하게 계속 돈다 - run_game이 sim.step()을 마친 뒤 렌더 단계에서
    겹쳐 그릴 뿐이다.
    """
    labels = [ELEMENT_NAMES[el] for el in PALETTE_ORDER]
    left_w = max(len(name) for name in labels) + 4  # 스와치 2칸 + 공백 + 이름
    right_w = max(len(PALETTE_RIGHT_HEADER), len(PALETTE_RIGHT_HINT), 14)
    gap = 3
    box_w = min(max_x - 2, left_w + gap + right_w)
    box_h = min(max_y - 2, len(PALETTE_ORDER) + 2)
    top = max(0, (max_y - box_h) // 2)
    left = max(0, (max_x - box_w) // 2)
    right_x = min(left + left_w + gap, max(0, max_x - 1))

    ui_pair = curses.color_pair(pairs.get_pair(UI_FG))
    active_attr = ui_pair | curses.A_BOLD
    dim_attr = ui_pair | curses.A_DIM
    left_active = state.palette_col == 0
    right_active = state.palette_col == 1

    try:
        stdscr.addstr(top, left, "원소"[:box_w], active_attr if left_active else dim_attr)
    except curses.error:
        pass
    try:
        stdscr.addstr(top, right_x, PALETTE_RIGHT_HEADER[: max(0, box_w - (right_x - left))],
                      active_attr if right_active else dim_attr)
    except curses.error:
        pass

    # --- 왼쪽 열: 원소 목록 ---
    for i, el in enumerate(PALETTE_ORDER[: box_h - 1]):
        row = top + 1 + i
        name = ELEMENT_NAMES[el]
        swatch_attr = curses.color_pair(pairs.get_pair(PALETTE_SWATCH_COLOR[el]))
        if left_active:
            swatch_attr |= curses.A_BOLD
            text_attr = ui_pair
            if i == state.palette_index:
                swatch_attr |= curses.A_REVERSE
                text_attr |= curses.A_REVERSE | curses.A_BOLD
        else:
            swatch_attr |= curses.A_DIM
            text_attr = dim_attr
        try:
            stdscr.addstr(row, left, "██", swatch_attr)
            stdscr.addstr(row, left + 3, name[: max(0, left_w - 3)], text_attr)
        except curses.error:
            pass

    # --- 오른쪽 열: 브러시 크기(숫자 + 반경만큼 점 미리보기) ---
    radius = state.brush_radius
    preview = ("●" * radius) if radius > 0 else "(없음)"
    right_lines = [f"크기: {radius}", preview[:right_w], PALETTE_RIGHT_HINT]
    for i, text in enumerate(right_lines):
        row = top + 1 + i
        if row >= top + box_h:
            break
        if right_active:
            attr = (active_attr | curses.A_REVERSE) if i == 0 else active_attr
        else:
            attr = dim_attr
        try:
            stdscr.addstr(row, right_x, text[: max(0, box_w - (right_x - left))], attr)
        except curses.error:
            pass


def draw_hud(stdscr, hud_row, max_x, state, pairs, fps, is_drawing):
    """상단(또는 지정 행)에 선택 원소/브러시/일시정지/FPS/힌트를 표시한다.

    항상 검정 배경 위에서 읽히도록 밝은 UI_FG 색 하나로 통일한다(개별 원소
    색을 쓰면 어두운 셰이드가 검정 배경에서 잘 안 보이는 경우가 있어서).
    """
    name = ELEMENT_NAMES[state.selected]
    parts = [
        f" 원소:{name}",
        f"브러시:{state.brush_radius}",
    ]
    if is_drawing:
        parts.append("그리기 ON")
    if state.paused:
        parts.append("PAUSED")
    if fps is not None:
        parts.append(f"{fps:.0f}FPS")
    # 종료 힌트("q:종료")는 화면에서 뺐다 - q/ESC 종료 키 자체(handle_key)는
    # 그대로 동작한다, HUD 표시만 안 할 뿐이다.
    parts.append("[/:원소]")
    line = " | ".join(parts)
    attr = curses.color_pair(pairs.get_pair(UI_FG)) | curses.A_BOLD
    try:
        stdscr.addstr(hud_row, 0, " " * max(0, max_x - 1))
        stdscr.addstr(hud_row, 0, line[: max_x - 1], attr)
    except curses.error:
        pass


# ============================================================
# curses 렌더 / 입력 루프
# ============================================================

MIN_TERM_COLS = 40
MIN_TERM_ROWS = 12


def _too_small(max_y, max_x):
    return max_x < MIN_TERM_COLS or max_y < MIN_TERM_ROWS


def _wait_for_bigger_terminal(stdscr):
    """터미널이 너무 작으면 크래시 대신 안내 메시지를 띄우고 대기한다."""
    stdscr.nodelay(True)
    while True:
        max_y, max_x = stdscr.getmaxyx()
        if not _too_small(max_y, max_x):
            return
        stdscr.erase()
        msg = f"터미널을 더 크게 해주세요 (최소 {MIN_TERM_COLS}x{MIN_TERM_ROWS}, 현재 {max_x}x{max_y})"
        try:
            stdscr.addstr(0, 0, msg[: max(0, max_x - 1)])
        except curses.error:
            pass
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), 27):
            raise SystemExit(0)
        time.sleep(0.05)


def _make_sim_for_screen(max_y, max_x, rng, old_sim=None):
    """터미널 크기에 맞춰 SandSim을 만든다. 기존 내용은 best-effort로 보존.

    문자 셀 1개 = octant 2x4 sim 픽셀이므로 가로는 max_x*2, 세로는
    (visible_rows)*4 (마지막 한 줄은 HUD로 비워둔다).
    """
    sim_rows_screen = max_y - 1  # 마지막 한 줄은 HUD
    width = max_x * 2
    height = sim_rows_screen * 4
    sim = SandSim(width, height, rng=rng)
    if old_sim is not None:
        copy_w = min(width, old_sim.width)
        copy_h = min(height, old_sim.height)
        for y in range(copy_h):
            for x in range(copy_w):
                sim.grid[y][x] = old_sim.grid[y][x]
                sim.life[y][x] = old_sim.life[y][x]
                sim.bug_dir[y][x] = old_sim.bug_dir[y][x]
        # tick도 이어받아야 BUG_MOVE_INTERVAL/BUG_EAT_INTERVAL 등 (tick+x+y)
        # 위상 판정이 리사이즈 전후로 안 끊긴다(수정 4).
        sim.tick = old_sim.tick
    return sim


def handle_key(key, state, sim):
    """키 입력 1건을 상태/시뮬레이션에 반영한다.

    SPACE(그리기 켜기/끄기 토글)는 run_game의 입력 소진 루프에서 먼저
    가로채 처리하므로 이 함수까지 넘어오지 않는다.
    """
    if key in (ord("q"), 27):
        state.quit = True
    elif key == ord("p"):
        state.paused = not state.paused
    elif key == ord("c"):
        sim.clear()
    elif key == ord("["):
        state.brush_radius = max(BRUSH_RADIUS_MIN, state.brush_radius - 1)
    elif key == ord("]"):
        state.brush_radius = min(BRUSH_RADIUS_MAX, state.brush_radius + 1)
    elif key == ord("e"):
        state.selected = EMPTY
    elif ord("1") <= key <= ord("7"):
        state.selected = ELEMENT_KEY_ORDER[key - ord("1")]
    elif key in (curses.KEY_LEFT, ord("h")):
        state.cx -= MOVE_STEP_X
    elif key in (curses.KEY_RIGHT, ord("l")):
        state.cx += MOVE_STEP_X
    elif key in (curses.KEY_UP, ord("k")):
        state.cy -= MOVE_STEP_Y
    elif key in (curses.KEY_DOWN, ord("j")):
        state.cy += MOVE_STEP_Y
    state.clamp_cursor(sim.width, sim.height)


def handle_mouse(state, sim):
    """마우스 클릭/드래그 시 커서를 옮기고 즉시 칠한다. 미지원 터미널은 조용히 무시."""
    try:
        _, mx, my, _, bstate = curses.getmouse()
    except curses.error:
        return
    state.cx = mx * 2
    state.cy = my * 4
    state.clamp_cursor(sim.width, sim.height)
    click_mask = 0
    for name in ("BUTTON1_PRESSED", "BUTTON1_CLICKED", "REPORT_MOUSE_POSITION"):
        click_mask |= getattr(curses, name, 0)
    if bstate & click_mask:
        # paint_cell로 클릭/드래그한 셀을 항상 통째로 칠하고(수정 2a), 그 위에
        # paint_disc로 브러시 반경만큼 추가로 넓게 칠한다.
        sim.paint_cell(state.cx, state.cy, state.selected)
        sim.paint_disc(state.cx, state.cy, state.brush_radius, state.selected)


def run_game(stdscr):
    """curses 진입점: 초기화, 메인 루프(입력->물리->렌더), 종료 시 복원."""
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    except curses.error:
        pass

    _wait_for_bigger_terminal(stdscr)

    rng = random.Random()
    render_rng = random.Random()
    max_y, max_x = stdscr.getmaxyx()
    sim = _make_sim_for_screen(max_y, max_x, rng)
    state = GameState(sim.width, sim.height)
    pairs = ColorPairCache()
    try:
        # 화면 전체 배경을 순검정으로 강제(erase()도 이 배경으로 채워진다) -
        # Orca 등 터미널 테마의 기본 배경이 비치는 것을 막는다.
        stdscr.bkgd(" ", curses.color_pair(pairs.black_on_black_pair()))
    except curses.error:
        pass

    frame_duration = 1.0 / 28.0
    frame_times = []

    while not state.quit:
        frame_start = time.perf_counter()

        # --- 입력 처리: 이번 프레임에 쌓인 키를 모두 소진 ---
        while True:
            key = stdscr.getch()
            if key == -1:
                break
            if key == curses.KEY_RESIZE:
                # 리사이즈는 팔레트가 열려 있어도 항상 반영한다(환경 이벤트라
                # UI 모드와 무관하게 처리해야 화면 불일치가 안 생긴다).
                new_y, new_x = stdscr.getmaxyx()
                if _too_small(new_y, new_x):
                    _wait_for_bigger_terminal(stdscr)
                    new_y, new_x = stdscr.getmaxyx()
                sim = _make_sim_for_screen(new_y, new_x, rng, old_sim=sim)
                state.clamp_cursor(sim.width, sim.height)
                continue

            if key == ord("/"):
                # 팔레트 토글. 열 때는 항상 원소 열(col 0)에서 시작하고
                # palette_index를 현재 선택된 원소 위치로 맞춘다. 이미
                # 열려 있으면(다시 /) 선택 변경 없이 취소하고 닫는다.
                # 열 때 그리기도 꺼서(state.drawing=False) 팔레트를 보는 동안
                # 커서 위치에 계속 칠해지는 걸 막는다(수정 1).
                if state.palette_open:
                    state.palette_open = False
                else:
                    state.palette_open = True
                    state.palette_col = 0
                    state.drawing = False
                    try:
                        state.palette_index = PALETTE_ORDER.index(state.selected)
                    except ValueError:
                        state.palette_index = 0
                continue

            if state.palette_open:
                # 팔레트가 열려 있으면 팔레트 조작이 게임 입력보다 우선한다 -
                # 그리기 토글/커서 이동/마우스 칠하기 등은 여기서 전부 무시된다.
                # 2단 메뉴: col 0=원소 목록, col 1=브러시 크기.
                if key in (curses.KEY_UP, ord("k")):
                    if state.palette_col == 0:
                        state.palette_index = (state.palette_index - 1) % len(PALETTE_ORDER)
                    else:
                        state.brush_radius = min(BRUSH_RADIUS_MAX, state.brush_radius + 1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    if state.palette_col == 0:
                        state.palette_index = (state.palette_index + 1) % len(PALETTE_ORDER)
                    else:
                        state.brush_radius = max(BRUSH_RADIUS_MIN, state.brush_radius - 1)
                elif key in (curses.KEY_RIGHT, ord("l")):
                    state.palette_col = min(1, state.palette_col + 1)
                elif key in (curses.KEY_LEFT, ord("h")):
                    state.palette_col = max(0, state.palette_col - 1)
                elif key in (10, 13, curses.KEY_ENTER):
                    state.selected = PALETTE_ORDER[state.palette_index]
                    state.palette_open = False
                elif key == 27:  # Esc
                    state.palette_open = False
                elif ord("1") <= key <= ord("9"):
                    # 어느 열에 있든 숫자는 항상 원소 열 점프로 취급(단순 처리).
                    # PALETTE_ORDER가 9개(7원소+벌레+지우개)라 1-9로 전부 닿는다.
                    idx = key - ord("1")
                    if idx < len(PALETTE_ORDER):
                        state.palette_col = 0
                        state.palette_index = idx
                        state.selected = PALETTE_ORDER[idx]
                        state.palette_open = False
                continue

            if key == curses.KEY_MOUSE:
                handle_mouse(state, sim)
                continue
            if key == ord(" "):
                # 그리기 지속 토글. hold 흉내(spray_until 타임스탬프)는
                # 방향키를 같이 누르면 OS key-repeat가 방향키 쪽으로 넘어가
                # SPACE 반복이 끊겨 분사가 멈추는 문제가 있어 토글로 바꿨다.
                state.drawing = not state.drawing
                continue
            handle_key(key, state, sim)

        if state.quit:
            break

        if state.drawing and not state.palette_open:
            # paint_cell로 커서가 속한 브라유 셀 전체를 항상 통째로 칠/지우고,
            # paint_disc로 브러시 반경만큼 그 밖까지 추가로 넓게 칠한다(수정 2a).
            sim.paint_cell(state.cx, state.cy, state.selected)
            sim.paint_disc(state.cx, state.cy, state.brush_radius, state.selected)

        if not state.paused:
            sim.step()

        # --- 렌더 (문자 셀 1개 = octant 2x4 sim 픽셀) ---
        max_y, max_x = stdscr.getmaxyx()
        stdscr.erase()
        visible_rows = max_y - 1
        visible_cols = min(max_x, sim.width // 2)
        cursor_row = state.cy // 4
        cursor_col = state.cx // 2
        for row in range(visible_rows):
            if row * 4 >= sim.height:
                break
            for col in range(visible_cols):
                glyph, fg, bg = render_cell(sim, row, col, render_rng)
                pair = pairs.get_pair(fg, bg)
                attr = curses.color_pair(pair)
                if row == cursor_row and col == cursor_col:
                    attr |= curses.A_REVERSE
                try:
                    stdscr.addstr(row, col, glyph, attr)
                except curses.error:
                    pass

        frame_times.append(time.perf_counter() - frame_start)
        if len(frame_times) > 20:
            frame_times.pop(0)
        avg = sum(frame_times) / len(frame_times) if frame_times else frame_duration
        fps = 1.0 / avg if avg > 0 else 0.0

        draw_hud(stdscr, max_y - 1, max_x, state, pairs, fps, state.drawing)

        if state.palette_open:
            draw_palette(stdscr, max_y, max_x, state, pairs)

        stdscr.refresh()

        elapsed = time.perf_counter() - frame_start
        remaining = frame_duration - elapsed
        if remaining > 0:
            time.sleep(remaining)


# ============================================================
# Headless self-test (curses 없이 순수 시뮬 검증)
# ============================================================

def run_selftest():
    """--selftest: curses 없이 SandSim 물리 불변식을 검증한다."""
    results = []
    ok = True

    # 1) 공중의 SAND 덩어리가 낙하하는지
    try:
        rng = random.Random(12345)
        sim = SandSim(20, 20, rng=rng)
        for x in range(5, 10):
            sim.set(x, 2, SAND)

        def lowest_sand_row():
            rows = [y for y in range(sim.height) for x in range(sim.width) if sim.grid[y][x] == SAND]
            return max(rows) if rows else None

        before = lowest_sand_row()
        for _ in range(40):
            sim.step()
        after = lowest_sand_row()
        assert before is not None and after is not None, "모래 셀이 사라짐"
        assert after > before or after == sim.height - 1, (
            f"모래가 낙하하지 않음 (before={before}, after={after})"
        )
        results.append("PASS: SAND 낙하")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: SAND 낙하 - {e}")

    # 2) WATER 더미가 좌우로 확산되는지
    try:
        rng = random.Random(999)
        sim = SandSim(30, 15, rng=rng)
        for x in range(30):
            sim.set(x, 14, STONE)
        for y in range(9, 12):
            for x in range(13, 17):
                sim.set(x, y, WATER)

        def water_width():
            xs = [x for y in range(sim.height) for x in range(sim.width) if sim.grid[y][x] == WATER]
            return (max(xs) - min(xs) + 1) if xs else 0

        before_w = water_width()
        for _ in range(60):
            sim.step()
        after_w = water_width()
        assert after_w > before_w, f"물이 확산되지 않음 (before={before_w}, after={after_w})"
        results.append("PASS: WATER 확산")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: WATER 확산 - {e}")

    # 3) FIRE에 인접한 OIL이 점화되는지
    try:
        rng = random.Random(42)
        sim = SandSim(20, 10, rng=rng)
        for x in range(20):
            sim.set(x, 9, STONE)
        for x in range(8, 12):
            sim.set(x, 8, OIL)
        sim.set(7, 8, FIRE, life=15)

        def oil_count():
            return sum(1 for y in range(sim.height) for x in range(sim.width) if sim.grid[y][x] == OIL)

        before_oil = oil_count()
        for _ in range(5):
            sim.step()
        after_oil = oil_count()
        assert after_oil < before_oil, f"기름이 인화되지 않음 (before={before_oil}, after={after_oil})"
        results.append("PASS: OIL 인화")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: OIL 인화 - {e}")

    # 4) N스텝 예외 없이 실행되는지 (여러 원소 혼합, BUG 포함)
    try:
        rng = random.Random(7)
        sim = SandSim(40, 25, rng=rng)
        elements = [SAND, WATER, STONE, FIRE, OIL, STEAM, PLANT, BUG]
        for _ in range(200):
            x = rng.randrange(40)
            y = rng.randrange(25)
            el = rng.choice(elements)
            life = BUG_START_ENERGY if el == BUG else rng.randint(0, 15)
            sim.set(x, y, el, life=life)
        for _ in range(300):
            sim.step()
        results.append("PASS: 300스텝 예외 없이 실행")
    except Exception as e:  # noqa: BLE001 - self-test는 모든 예외를 포착해 보고해야 함
        ok = False
        results.append(f"FAIL: 예외 발생 - {type(e).__name__}: {e}")

    # 5) BUG가 인접 식물을 먹으면 PLANT 감소 + energy 증가
    try:
        rng = random.Random(11)
        sim = SandSim(15, 15, rng=rng)
        for x in range(15):
            sim.set(x, 14, STONE)
        sim.set(7, 13, BUG, life=BUG_START_ENERGY)
        sim.set(8, 13, PLANT)

        def plant_count():
            return sum(1 for yy in range(sim.height) for xx in range(sim.width) if sim.grid[yy][xx] == PLANT)

        before_plants = plant_count()
        # BUG_EAT_INTERVAL 게이트라 매 프레임 먹지 않으니 여유 있게 반복한다
        # (게이트는 tick+x+y 잔여값 기준이라 interval 안에서 한 번은 반드시 연다).
        ate = False
        for _ in range(BUG_EAT_INTERVAL + 5):
            sim.step()
            if plant_count() < before_plants:
                ate = True
                break
        bug_positions = [(xx, yy) for yy in range(sim.height) for xx in range(sim.width) if sim.grid[yy][xx] == BUG]
        assert ate, f"{BUG_EAT_INTERVAL + 5}스텝 내에 식물을 먹지 않음"
        assert len(bug_positions) == 1, f"벌레 개수 이상(1이어야 함, 실제 {len(bug_positions)})"
        bx, by = bug_positions[0]
        assert sim.life[by][bx] > BUG_START_ENERGY, f"먹었는데 energy가 늘지 않음({sim.life[by][bx]})"
        results.append("PASS: BUG 식물 섭취(PLANT 감소 + energy 증가)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: BUG 식물 섭취 - {e}")

    # 6) 먹이 없이 두면 굶어 죽어 사라짐(EMPTY)
    try:
        rng = random.Random(22)
        sim = SandSim(10, 10, rng=rng)
        for x in range(10):
            sim.set(x, 9, STONE)
        sim.set(5, 8, BUG, life=BUG_START_ENERGY)
        died = False
        # 대사만으로 굶어 죽으려면 최소 BUG_START_ENERGY * BUG_METABOLISM_INTERVAL
        # tick이 필요하다(현재 값 기준 140*8=1120) - 넉넉히 여유를 둔다.
        max_steps = BUG_START_ENERGY * BUG_METABOLISM_INTERVAL + 300
        for _ in range(max_steps):
            sim.step()
            remaining = sum(1 for yy in range(sim.height) for xx in range(sim.width) if sim.grid[yy][xx] == BUG)
            if remaining == 0:
                died = True
                break
        assert died, f"{max_steps}스텝 내에 굶어 죽지 않음"
        results.append("PASS: BUG 굶어 죽음(EMPTY로 소멸)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: BUG 굶어 죽음 - {e}")

    # 7) 불 옆(주변)의 벌레는 죽는다(FIRE로 타거나 소멸)
    try:
        rng = random.Random(33)
        sim = SandSim(10, 10, rng=rng)
        for x in range(10):
            sim.set(x, 9, STONE)
        sim.set(5, 8, BUG, life=BUG_START_ENERGY)
        # 도망갈 빈 칸이 아예 없도록 8이웃 중 갈 수 있는 방향을 전부 불/바닥으로
        # 막아 회피 로직과 상관없이 결정적으로 검증한다.
        for ddx, ddy in ((-1, 0), (1, 0), (0, -1), (-1, -1), (1, -1)):
            sim.set(5 + ddx, 8 + ddy, FIRE, life=FIRE_LIFE_MAX)
        died = False
        for _ in range(5):
            sim.step()
            remaining = sum(1 for yy in range(sim.height) for xx in range(sim.width) if sim.grid[yy][xx] == BUG)
            if remaining == 0:
                died = True
                break
        assert died, "불에 둘러싸인 벌레가 죽지 않음(여전히 BUG로 생존)"
        results.append("PASS: BUG 화염 인접 사망")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: BUG 화염 인접 사망 - {e}")

    # 8) 벌레는 한 step에 최대 한 칸만 이동한다(체비셰프 거리 <= 1)
    try:
        rng = random.Random(44)
        sim = SandSim(20, 20, rng=rng)
        for x in range(20):
            sim.set(x, 19, STONE)
        sim.set(10, 18, BUG, life=BUG_START_ENERGY)
        prev = (10, 18)
        max_one_tile = True
        for _ in range(30):
            sim.step()
            positions = [(xx, yy) for yy in range(sim.height) for xx in range(sim.width) if sim.grid[yy][xx] == BUG]
            if len(positions) != 1:
                break  # 죽거나 번식해 마릿수가 바뀌면 이 불변식 확인은 여기서 종료
            cx2, cy2 = positions[0]
            if max(abs(cx2 - prev[0]), abs(cy2 - prev[1])) > 1:
                max_one_tile = False
                break
            prev = (cx2, cy2)
        assert max_one_tile, "벌레가 한 step에 2칸 이상 이동함"
        results.append("PASS: BUG 스텝당 최대 1칸 이동")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: BUG 스텝당 최대 1칸 이동 - {e}")

    # 9) _set_dir/_get_dir 왕복 - 9방향 모두 정확히 복원되는지(수정 1 회귀 테스트)
    try:
        rng = random.Random(55)
        sim = SandSim(5, 5, rng=rng)
        bad = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                sim._set_dir(2, 2, dx, dy)
                got = sim._get_dir(2, 2)
                if got != (dx, dy):
                    bad = ((dx, dy), got)
                    break
            if bad is not None:
                break
        assert bad is None, f"방향 왕복 실패: 저장 {bad[0]} != 복원 {bad[1]}"
        results.append("PASS: BUG _set_dir/_get_dir 왕복(9방향)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: BUG _set_dir/_get_dir 왕복 - {e}")

    # 10) 벌레 바로 아래(다음 행)의 식물도 먹을 수 있는지(수정 3 회귀 테스트) -
    # 그 행은 이번 프레임에 이미 처리 끝난 행이라 processed 체크만으론 막혀선 안 됨.
    try:
        rng = random.Random(66)
        sim = SandSim(10, 10, rng=rng)
        for x in range(10):
            sim.set(x, 9, STONE)
        sim.set(5, 6, BUG, life=BUG_START_ENERGY)
        sim.set(5, 7, PLANT)

        def plant_count_below():
            return sum(1 for yy in range(sim.height) for xx in range(sim.width) if sim.grid[yy][xx] == PLANT)

        before_plants = plant_count_below()
        ate_below = False
        for _ in range(BUG_EAT_INTERVAL + 5):
            sim.step()
            if plant_count_below() < before_plants:
                ate_below = True
                break
        assert ate_below, f"{BUG_EAT_INTERVAL + 5}스텝 내에 바로 아래 식물을 먹지 않음"
        results.append("PASS: BUG 바로 아래 식물 섭취(아래 행 processed 무시)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: BUG 바로 아래 식물 섭취 - {e}")

    # 11) 대각선으로 맞닿은 OIL/WATER가 진동하지 않는지(수정 2 회귀 테스트) -
    # 완전히 STONE으로 둘러싸 이 둘의 대각선 상호작용만 남긴 최소 배치.
    try:
        rng = random.Random(77)
        sim = SandSim(15, 15, rng=rng)
        for yy in range(15):
            for xx in range(15):
                sim.set(xx, yy, STONE)
        sim.set(5, 5, OIL)
        sim.set(6, 6, WATER)
        stable = True
        for _ in range(20):
            sim.step()
            if sim.grid[5][5] != OIL or sim.grid[6][6] != WATER:
                stable = False
                break
        assert stable, "대각선으로 맞닿은 OIL/WATER 위치가 뒤집힘(진동)"
        results.append("PASS: OIL/WATER 대각선 비진동")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: OIL/WATER 대각선 비진동 - {e}")

    # 12) 벌레가 세로 식물 기둥을 먹으며 타고 올라가는지(climb, 이번 수정 회귀
    # 테스트) - 좌우는 STONE 벽으로 막아 옆으로 못 새게 하되, 시작 칸
    # 아래는 일부러 바닥 없이 길게 열어둔다(그리드 맨 아래까지). "매달림"
    # 없이 매번 떨어지는 옛 버전이면 첫 비섭식 tick에 바로 시야 밖까지
    # 추락해 다시는 못 올라오므로(실측 확인됨), 이 배치가 진짜로 두 버전을
    # 가른다 - 바닥이 바로 밑에 있으면 옛 버전도 배회로 슬금슬금 복구돼
    # 버그를 못 잡는다.
    try:
        rng = random.Random(88)
        sim = SandSim(10, 150, rng=rng)
        for yy in range(150):
            sim.set(4, yy, STONE)
            sim.set(6, yy, STONE)
        for yy in range(10, 25):
            sim.set(5, yy, PLANT)  # 세로 식물 기둥(10~24행, 15칸)
        start_y = 25
        sim.set(5, start_y, BUG, life=BUG_START_ENERGY)
        # (5, start_y) 아래는 전부 EMPTY - 그리드 바닥(149행)까지 뚫린 낙하 통로.

        def plant_count_climb():
            return sum(1 for yy in range(sim.height) for xx in range(sim.width) if sim.grid[yy][xx] == PLANT)

        def bug_ys():
            return [yy for yy in range(sim.height) for xx in range(sim.width) if sim.grid[yy][xx] == BUG]

        before_plants = plant_count_climb()
        min_y_reached = start_y
        for _ in range(200):
            sim.step()
            ys = bug_ys()
            if not ys:
                break
            min_y_reached = min(min_y_reached, min(ys))
        after_plants = plant_count_climb()
        assert min_y_reached < start_y, f"벌레가 위로 못 올라감(최고 도달 y={min_y_reached}, 시작 y={start_y})"
        assert after_plants < before_plants, "식물을 하나도 안 먹음"
        results.append("PASS: BUG 식물 기둥 타고 오르기(climb)")
    except AssertionError as e:
        ok = False
        results.append(f"FAIL: BUG 식물 기둥 타고 오르기 - {e}")

    for line in results:
        print(line)

    if ok:
        print("SELFTEST PASS")
        return 0
    print("SELFTEST FAIL: 하나 이상의 검증 실패 (위 FAIL 항목 참고)")
    return 1


# ============================================================
# 진입점
# ============================================================

USAGE = """사용법: falling_sand.py [--selftest | --help]

터미널 폴링샌드(falling-sand) 물리 샌드박스 게임 (curses 기반).

옵션:
  (없음)       게임 실행
  --selftest   curses 없이 물리 시뮬레이션 자기테스트만 실행
  --help, -h   이 도움말 출력

조작 요약:
  화살표 / h j k l   커서 이동
  SPACE              그리기 켜기/끄기 (켜면 커서가 움직이는 대로 그려짐)
  1-7                원소 선택 (모래/물/돌/불/기름/증기/식물), 또는 /로 목록에서 선택
  /                  원소/브러시 팔레트 열기 (←→ 열 전환, ↑↓ 조작, Enter 확정, Esc 취소)
  e                  지우개
  [ / ]              브러시 크기 축소/증가
  c                  전체 지우기
  p                  일시정지/재개
  q / ESC            종료
"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    if "--selftest" in argv:
        return run_selftest()
    try:
        curses.wrapper(run_game)
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
