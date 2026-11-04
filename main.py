"""
六爻排盘与 AI 解卦 Agent —— 后端服务
========================================
模块：
  1. 时间与天干地支模块（lunar-python）
  2. 严谨起卦算法（三枚铜钱 × 6 次）
  3. 六神排盘模块（日干 → 初爻六神 → 逐爻推算）
  4. 知识库管理（hexagrams.json）
  5. AI 解卦引擎（OpenAI 兼容 API）
"""

import os
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import httpx
from lunar_python import Solar

# ── 加载环境变量 ──
load_dotenv()

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ── FastAPI 应用 ──
app = FastAPI(title="AI 六爻排盘解卦 Agent", version="2.1.0")


@app.middleware("http")
async def security_headers(request, call_next):
    """基础安全响应头：防 MIME 嗅探 / 防点击劫持 / 不泄露来源"""
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ── 请求参数日志（仅记录排盘类接口的卦码，便于核对前后端传参）──
@app.middleware("http")
async def log_paipan_params(request, call_next):
    if request.method == "POST" and request.url.path in ("/api/paipan", "/api/divine"):
        body = await request.body()
        try:
            data = json.loads(body)
            gr = data.get("gua_result") or {}
            ben_code = data.get("ben_gua_code") or gr.get("ben_gua_code", "")
            bian_code = data.get("bian_gua_code") or gr.get("bian_gua_code", "")
            print(f"[LOG][{request.url.path}] ben_gua_code={ben_code!r} bian_gua_code={bian_code!r}", flush=True)
        except Exception:
            # 仅记录长度，避免把用户问题等正文写入日志
            print(f"[LOG][{request.url.path}] 非 JSON 请求体，长度={len(body)}", flush=True)
    return await call_next(request)


# ====================================================================
# 模块一：时间与天干地支模块
# ====================================================================


def get_ganzhi_info(now: Optional[datetime] = None) -> dict:
    """
    获取当前时间的农历及干支纪时信息。
    返回：月建（月柱干支）、日辰（日柱干支）、旬空（空亡）
    """
    if now is None:
        now = datetime.now()

    solar = Solar.fromYmdHms(
        now.year, now.month, now.day,
        now.hour, now.minute, now.second
    )
    lunar = solar.getLunar()
    eight_char = lunar.getEightChar()
    richen = lunar.getDayInGanZhi()

    return {
        "solar_date": f"{now.year}年{now.month:02d}月{now.day:02d}日 {now.hour:02d}:{now.minute:02d}",
        "lunar_date": f"农历{lunar.getYearInChinese()}年{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
        "lunar_year": lunar.getYearInChinese(),
        "lunar_month": lunar.getMonthInChinese(),
        "lunar_day": lunar.getDayInChinese(),
        "nianzhu": lunar.getYearInGanZhi(),          # 年柱
        "year_ganzhi": lunar.getYearInGanZhi(),
        "yuezhu": lunar.getMonthInGanZhi(),          # 月柱
        "yuejian": lunar.getMonthInGanZhi(),         # 月建（同月柱）
        "rizhu": richen,                              # 日柱
        "richen": richen,                             # 日辰（同日柱）
        "rigan": richen[0],                           # 日干（取日辰首字）
        "shizhu": lunar.getTimeInGanZhi(),            # 时柱
        "shichen": lunar.getTimeInGanZhi(),           # 时辰干支
        "xunkong": eight_char.getDayXunKong(),        # 旬空（空亡）
        "day_xun": eight_char.getDayXun(),            # 日旬
    }


def parse_qigua_time(s: Optional[str]) -> Optional[datetime]:
    """解析前端 datetime-local 字符串（YYYY-MM-DDTHH:MM），秒缺省补 0；非法输入返回 None。"""
    if not s:
        return None
    s = str(s).strip()
    try:
        if "T" in s:
            date_part, time_part = s.split("T", 1)
        else:
            date_part, time_part = s, "00:00"
        tp = time_part.split(":")
        hh = int(tp[0]) if len(tp) > 0 and tp[0] else 0
        mm = int(tp[1]) if len(tp) > 1 and tp[1] else 0
        ss = int(tp[2]) if len(tp) > 2 and tp[2] else 0
        return datetime(int(date_part[:4]), int(date_part[5:7]), int(date_part[8:10]), hh, mm, ss)
    except (ValueError, IndexError):
        return None


# ====================================================================
# 模块二：爻象常量（摇卦/排盘共用）
# ====================================================================

# 爻象常量与符号
LINE_INFO = {
    6: {"name": "老阴", "yin_yang": 0, "is_moving": True,  "symbol": "⚋", "moving_mark": "⨅"},
    7: {"name": "少阳", "yin_yang": 1, "is_moving": False, "symbol": "⚊", "moving_mark": ""},
    8: {"name": "少阴", "yin_yang": 0, "is_moving": False, "symbol": "⚋", "moving_mark": ""},
    9: {"name": "老阳", "yin_yang": 1, "is_moving": True,  "symbol": "⚊", "moving_mark": "⨀"},
}

# 爻位名称（从下到上）
YAO_POSITIONS = ["初爻", "二爻", "三爻", "四爻", "五爻", "上爻"]


# ====================================================================
# 模块三：六神（六兽）排盘模块
# ====================================================================

# 六神固定循环顺序（青龙 → 朱雀 → 勾陈 → 腾蛇 → 白虎 → 玄武）
LIU_SHEN_ORDER = ["青龙", "朱雀", "勾陈", "腾蛇", "白虎", "玄武"]

# 日干 → 初爻六神 映射表
RIGAN_LIUSHEN: dict[str, str] = {
    "甲": "青龙", "乙": "青龙",
    "丙": "朱雀", "丁": "朱雀",
    "戊": "勾陈",
    "己": "腾蛇",
    "庚": "白虎", "辛": "白虎",
    "壬": "玄武", "癸": "玄武",
}


def assign_liushen(rigan: str) -> list[str]:
    """
    根据日干决定初爻六神，按固定循环顺序推算全部六爻。
    返回长度为 6 的列表，索引 0 = 初爻，索引 5 = 上爻。
    """
    start_shen = RIGAN_LIUSHEN.get(rigan, "青龙")
    start_idx = LIU_SHEN_ORDER.index(start_shen)
    return [LIU_SHEN_ORDER[(start_idx + i) % 6] for i in range(6)]


# ====================================================================
# 模块四：纳甲排盘算法（浑天甲子）
# ====================================================================

# 八宫八卦纳甲表：{ 三爻码: (内卦三地支, 外卦三地支) }
# 每项为 (地支, 五行)
NAJIA_MAP: dict = {
    "111": (  # 乾
        [("子", "水"), ("寅", "木"), ("辰", "土")],
        [("午", "火"), ("申", "金"), ("戌", "土")],
    ),
    "010": (  # 坎
        [("寅", "木"), ("辰", "土"), ("午", "火")],
        [("申", "金"), ("戌", "土"), ("子", "水")],
    ),
    "001": (  # 艮
        [("辰", "土"), ("午", "火"), ("申", "金")],
        [("戌", "土"), ("子", "水"), ("寅", "木")],
    ),
    "100": (  # 震（与乾同）
        [("子", "水"), ("寅", "木"), ("辰", "土")],
        [("午", "火"), ("申", "金"), ("戌", "土")],
    ),
    "011": (  # 巽
        [("丑", "土"), ("亥", "水"), ("酉", "金")],
        [("未", "土"), ("巳", "火"), ("卯", "木")],
    ),
    "101": (  # 离
        [("卯", "木"), ("丑", "土"), ("亥", "水")],
        [("酉", "金"), ("未", "土"), ("巳", "火")],
    ),
    "000": (  # 坤
        [("未", "土"), ("巳", "火"), ("卯", "木")],
        [("丑", "土"), ("亥", "水"), ("酉", "金")],
    ),
    "110": (  # 兑
        [("巳", "火"), ("卯", "木"), ("丑", "土")],
        [("亥", "水"), ("酉", "金"), ("未", "土")],
    ),
}


def assign_najia(gua_code: str) -> list[dict]:
    """
    根据卦码为本卦/变卦的每一爻分配地支与五行。
    返回长度为 6 的列表，索引 0=初爻 … 5=上爻。
    """
    inner_code = gua_code[:3]
    outer_code = gua_code[3:]
    inner_zhi = NAJIA_MAP[inner_code][0]
    outer_zhi = NAJIA_MAP[outer_code][1]
    result = []
    for i in range(3):
        result.append({"dizhi": inner_zhi[i][0], "wuxing": inner_zhi[i][1]})
    for i in range(3):
        result.append({"dizhi": outer_zhi[i][0], "wuxing": outer_zhi[i][1]})
    return result


# ====================================================================
# 模块五：六亲计算逻辑
# ====================================================================

# 五行生克关系表：(我, 对方) -> 关系
_WX_RELATION: dict = {
    ("木", "火"): "我生", ("火", "木"): "生我",
    ("火", "土"): "我生", ("土", "火"): "生我",
    ("土", "金"): "我生", ("金", "土"): "生我",
    ("金", "水"): "我生", ("水", "金"): "生我",
    ("水", "木"): "我生", ("木", "水"): "生我",
    ("木", "土"): "我克", ("土", "木"): "克我",
    ("土", "水"): "我克", ("水", "土"): "克我",
    ("水", "火"): "我克", ("火", "水"): "克我",
    ("火", "金"): "我克", ("金", "火"): "克我",
    ("金", "木"): "我克", ("木", "金"): "克我",
}


def calculate_liuqin(palace_element: str, line_element: str) -> str:
    """宫五行（我）vs 爻五行（他）→ 六亲"""
    if palace_element == line_element:
        return "兄弟"
    rel = _WX_RELATION.get((palace_element, line_element), "")
    if rel == "生我":
        return "父母"
    elif rel == "我生":
        return "子孙"
    elif rel == "克我":
        return "官鬼"
    elif rel == "我克":
        return "妻财"
    return "兄弟"


def assign_liuqin_for_hexagram(gua_code: str, lines_with_najia: list, palace_element_override: str = None) -> list[str]:
    """
    为整卦的每一爻计算六亲。
    palace_element_override: 若传入，强制以此宫五行为"我"，不查卦码自身的卦宫。
    用于变卦六亲计算 —— 变爻不离本宫，变卦六亲必须以本卦宫五行为基准。
    """
    if palace_element_override is not None:
        palace_element = palace_element_override
    else:
        gua_info = HEXAGRAMS_DB.get(gua_code, {})
        palace_element = gua_info.get("element", "?")
    liuqin_list = []
    for line in lines_with_najia:
        liuqin_list.append(calculate_liuqin(palace_element, line["wuxing"]))
    return liuqin_list


# ====================================================================
# 模块六：世爻与应爻推算模块
# ====================================================================

def assign_shiying(gua_code: str) -> list[str]:
    """
    根据卦码计算每爻的世应标记。
    返回长度为 6 的列表，索引 0=初爻 … 5=上爻。
    每项为 "世" / "应" / ""。
    应爻位置 = (世爻位置 + 2) % 6 + 1
    """
    gua_info = HEXAGRAMS_DB.get(gua_code, {})
    shi_pos = gua_info.get("shi_position", 6)
    ying_pos = (shi_pos + 2) % 6 + 1  # 隔两爻为应
    result = []
    for i in range(6):
        pos = i + 1  # 1-based position
        if pos == shi_pos:
            result.append("世")
        elif pos == ying_pos:
            result.append("应")
        else:
            result.append("")
    return result


# ====================================================================
# 模块七：神煞推算模块
# ====================================================================

# 天乙贵人映射：日干 → 贵人地支集合
_TIANYI_MAP: dict[str, set] = {
    "甲": {"丑", "未"}, "戊": {"丑", "未"}, "庚": {"丑", "未"},
    "乙": {"子", "申"}, "己": {"子", "申"},
    "丙": {"亥", "酉"}, "丁": {"亥", "酉"},
    "壬": {"卯", "巳"}, "癸": {"卯", "巳"},
    "辛": {"午", "寅"},
}

# 驿马映射：日支（三合局）→ 驿马地支
_YIMA_MAP: dict[str, str] = {
    "申": "寅", "子": "寅", "辰": "寅",
    "寅": "申", "午": "申", "戌": "申",
    "亥": "巳", "卯": "巳", "未": "巳",
    "巳": "亥", "酉": "亥", "丑": "亥",
}

# 桃花/咸池映射：日支（三合局）→ 桃花地支
_TAOHUA_MAP: dict[str, str] = {
    "申": "酉", "子": "酉", "辰": "酉",
    "寅": "卯", "午": "卯", "戌": "卯",
    "亥": "子", "卯": "子", "未": "子",
    "巳": "午", "酉": "午", "丑": "午",
}


def calculate_shensha(ri_gan: str, ri_zhi: str, yao_zhi: str) -> list[str]:
    """
    根据日干、日支与爻的地支，计算该爻触发的神煞。
    返回触发神煞的名称列表（可为空）。
    """
    result = []
    # 天乙贵人：看日干，爻之地支是否在贵人集合中
    tianyi_set = _TIANYI_MAP.get(ri_gan, set())
    if yao_zhi in tianyi_set:
        result.append("天乙贵人")

    # 驿马：看日支，爻之地支是否等于驿马地支
    yima_zhi = _YIMA_MAP.get(ri_zhi, "")
    if yao_zhi == yima_zhi:
        result.append("驿马")

    # 桃花/咸池：看日支
    taohua_zhi = _TAOHUA_MAP.get(ri_zhi, "")
    if yao_zhi == taohua_zhi:
        result.append("桃花")

    return result


# ====================================================================
# 模块八：知识库管理模块
# ====================================================================

HEXAGRAMS_DB: dict = {}

BASE_DIR = Path(__file__).parent
HEXAGRAMS_PATH = BASE_DIR / "hexagrams.json"


def load_hexagrams() -> dict:
    """加载六十四卦知识库"""
    global HEXAGRAMS_DB
    with open(HEXAGRAMS_PATH, "r", encoding="utf-8") as f:
        HEXAGRAMS_DB = json.load(f)
    return HEXAGRAMS_DB


def lookup_gua(code: str) -> dict:
    """根据二元码查找卦象信息"""
    gua = HEXAGRAMS_DB.get(code)
    if gua is None:
        return {
            "index": 0,
            "name": "未知卦象",
            "upper": "?",
            "lower": "?",
            "upper_symbol": "?",
            "lower_symbol": "?",
            "palace": "?",
            "element": "?",
            "statement": "",
        }
    return gua


# 启动时加载
load_hexagrams()


# ====================================================================
# 模块八·B：焦氏易林知识库（4096 条林辞）
# ====================================================================

JIAO_YILIN_PATH = BASE_DIR / "jiao_yilin.json"
JIAO_YILIN_DB: dict = {}

# 周易六十四卦标准短名（与 jiao_yilin.json 键名一致）
GUA_ORDER = ["乾","坤","屯","蒙","需","讼","师","比","小畜","履","泰","否","同人","大有",
             "谦","豫","随","蛊","临","观","噬嗑","贲","剥","复","无妄","大畜","颐","大过",
             "坎","离","咸","恒","遁","大壮","晋","明夷","家人","睽","蹇","解","损","益",
             "夬","姤","萃","升","困","井","革","鼎","震","艮","渐","归妹","丰","旅","巽","兑",
             "涣","节","中孚","小过","既济","未济"]


def load_jiao_yilin() -> dict:
    """加载焦氏易林全辞（4096 条，键=「本卦之变卦」）"""
    global JIAO_YILIN_DB
    with open(JIAO_YILIN_PATH, "r", encoding="utf-8") as f:
        JIAO_YILIN_DB = json.load(f)
    return JIAO_YILIN_DB


def full_to_short(gua_name: str) -> str:
    """将 hexagrams.json 的全称卦名转换为周易短名（用于易林查询）。
    例：「风天小畜」→「小畜」；「乾为天」→「乾」。
    已为短名则原样返回。"""
    if gua_name in GUA_ORDER:
        return gua_name
    if "为" in gua_name:
        return gua_name[0]
    for name in GUA_ORDER:
        if gua_name.endswith(name):
            return name
    return gua_name


def lookup_jiao(ben_gua_name: str, bian_gua_name: str) -> str:
    """根据本卦/变卦名查找焦氏易林林辞；未找到返回空字符串"""
    return JIAO_YILIN_DB.get(f"{full_to_short(ben_gua_name)}之{full_to_short(bian_gua_name)}", "")


load_jiao_yilin()


# ====================================================================
# 模块：卦爻辞知识库 + 动爻取用法则
# ====================================================================
YAO_CI_PATH = BASE_DIR / "yao_ci.json"
YAO_CI_DB: dict = {}


def load_yao_ci() -> dict:
    """加载六十四卦爻辞：键=卦全称，值=[初爻..上爻] 六条爻辞"""
    global YAO_CI_DB
    with open(YAO_CI_PATH, "r", encoding="utf-8") as f:
        YAO_CI_DB = json.load(f)
    return YAO_CI_DB


def compute_dong_yao(lines) -> list:
    return sorted([i for i, l in enumerate(lines) if l.get("is_moving")])


def compute_yao_info(lines, ben_gua_name: str, bian_gua_name: str) -> dict:
    """按「动爻取用法则」定取用主爻，并备好本卦/变卦爻辞。
    规则：0动看彖辞(卦辞)；1动取独发；2动取阴，同阴同阳取上动；
          3动取中爻；4动取下静；5动取独静；6动乾坤用九/用六，其余参彖辞+变卦爻辞。
    返回信息供前端展示与 AI prompt 注入。"""
    dong = compute_dong_yao(lines)
    n = len(dong)
    allidx = list(range(6))
    idx = None
    rule = ""
    special = ""

    if n == 0:
        rule = "静卦无动爻：以本卦彖辞（卦辞）与整体卦象取义，安分守常。"
    elif n == 1:
        idx = dong[0]
        rule = "一爻独发：取此独发之动爻为用神枢机。"
    elif n == 2:
        duo = [lines[i]["yin_yang"] for i in dong]
        if duo[0] == duo[1]:
            idx = max(dong)
            rule = "两爻动且同阴同阳：取上动之爻（位高者）为用。"
        else:
            idx = dong[0] if duo[0] == 0 else dong[1]
            rule = "两爻动（一阴一阳）：取其中之阴爻为用。"
    elif n == 3:
        idx = sorted(dong)[1]
        rule = "三爻动：取居中之动爻（中爻）为用。"
    elif n == 4:
        jing = sorted(i for i in allidx if i not in set(dong))
        idx = jing[0]
        rule = "四爻动：取下位之静爻为用。"
    elif n == 5:
        jing = [i for i in allidx if i not in set(dong)]
        idx = jing[0]
        rule = "五爻动：取独静之爻为用。"
    else:  # n == 6
        if ben_gua_name == "乾为天":
            rule = "乾卦六爻皆动：用九『见群龙无首，吉』——刚极能变，反见吉祥。"
            special = "乾卦用九；刚健之极，戒亢宜随时自处。"
        elif ben_gua_name == "坤为地":
            rule = "坤卦六爻皆动：用六『利永贞』——柔顺之极，宜守正久安。"
            special = "坤卦用六；纯阴之地，以柔顺守贞为吉。"
        else:
            rule = "六爻皆动（乾坤除外）：参看本卦彖辞（卦辞）与变卦爻辞合断吉凶。"
            special = "六爻纠缠、情势杂沓，宜以所见卦致（彖）断之，并纳变卦爻辞象意。"

    ben_yao = YAO_CI_DB.get(ben_gua_name) or ["" for _ in range(6)]
    bian_yao = YAO_CI_DB.get(bian_gua_name) or ["" for _ in range(6)]

    ben_dong_ci = [f"{YAO_POSITIONS[i]}『{ben_yao[i]}』" for i in dong if i < len(ben_yao) and ben_yao[i]]
    bian_dong_ci = [f"{YAO_POSITIONS[i]}『{bian_yao[i]}』" for i in dong if i < len(bian_yao) and bian_yao[i]]
    zhu_ci = ben_yao[idx] if (idx is not None and idx < len(ben_yao) and ben_yao[idx]) else ""

    return {
        "dong_count": n,
        "zhugong_idx": idx,
        "zhugong_pos": YAO_POSITIONS[idx] if idx is not None else None,
        "zhugong_rule": rule,
        "special": special,
        "zhugong_ci": zhu_ci,
        "ben_dong_yao_ci": ben_dong_ci,
        "bian_dong_yao_ci": bian_dong_ci,
    }


# 五行生克关系
SHENG = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
KE = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}

# ── 地支关系常量表 ──
DIZHI_CHONG = {"子": "午", "午": "子", "丑": "未", "未": "丑",
               "寅": "申", "申": "寅", "卯": "酉", "酉": "卯",
               "辰": "戌", "戌": "辰", "巳": "亥", "亥": "巳"}
DIZHI_HE = {"子": "丑", "丑": "子", "寅": "亥", "亥": "寅",
            "卯": "戌", "戌": "卯", "辰": "酉", "酉": "辰",
            "巳": "申", "申": "巳", "午": "未", "未": "午"}
# 六害（六穿）：子未、丑午、寅巳、卯辰、申亥、酉戌，主暗中损害
DIZHI_HAI = {"子": "未", "未": "子", "丑": "午", "午": "丑",
             "寅": "巳", "巳": "寅", "卯": "辰", "辰": "卯",
             "申": "亥", "亥": "申", "酉": "戌", "戌": "酉"}
# 三刑组：无恩之刑(寅巳申)、恃势之刑(丑戌未)、无礼之刑(子卯)、自刑(辰午酉亥)
XING_GROUPS = [
    ("寅", "巳", "申"),
    ("丑", "戌", "未"),
    ("子", "卯"),
    ("辰", "辰"), ("午", "午"), ("酉", "酉"), ("亥", "亥"),
]
# 三合局（三支齐=三合，两支=半合）
SANHE_GROUPS = {
    ("申", "子", "辰"): "水局",
    ("亥", "卯", "未"): "木局",
    ("寅", "午", "戌"): "火局",
    ("巳", "酉", "丑"): "金局",
}


def compute_gua_relations(lines, yuejian_zhi: str, richen_zhi: str,
                          bian_code: str = "", bian_zhis: Optional[list] = None) -> dict:
    """卦与爻层面的合冲刑破关系：
    - 六冲卦 / 六合卦（按六爻纳甲地支逐位判定），含变卦的六冲/六合标记
    - 爻间六合、六冲、六害、三刑、三合局、半合局
    - 动爻化出之爻与其余各爻的六合/六冲/六害（化爻取象用）
    - 月破 / 日破（爻支被月建或日辰地支相冲）"""
    zhis = [l.get("najia_ben", {}).get("dizhi", "?") for l in lines]
    poss = [l.get("position", "") for l in lines]
    rel = {"liu_chong": False, "liu_he": False,
           "bian_liu_chong": False, "bian_liu_he": False,
           "chong_text": [], "he_text": [], "hai_text": [], "xing_text": [],
           "sanhe_text": [], "banhe_text": [], "po_text": []}

    # 六冲卦：初↔四、二↔五、三↔上 三对地支皆相冲（八纯卦+无妄+大壮共十卦）
    if all(DIZHI_CHONG.get(a) == b for a, b in zip(zhis[:3], zhis[3:])):
        rel["liu_chong"] = True
    # 六合卦：六爻阴阳两两相应（全异位），即否泰既济未济咸恒损益八卦
    if all(lines[i]["yin_yang"] != lines[i + 3]["yin_yang"] for i in range(3)):
        rel["liu_he"] = True

    # 变卦的六冲/六合（变卦按纳甲地支逐位相冲、爻象全异位判定）
    if bian_code and bian_zhis:
        if all(DIZHI_CHONG.get(a) == b for a, b in zip(bian_zhis[:3], bian_zhis[3:])):
            rel["bian_liu_chong"] = True
        if all(bian_code[i] != bian_code[i + 3] for i in range(3)):
            rel["bian_liu_he"] = True

    # 爻间六合 / 六冲（任意两爻）
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a, b = zhis[i], zhis[j]
            if a == "?" or b == "?":
                continue
            if DIZHI_HE.get(a) == b:
                rel["he_text"].append(f"{poss[i]}{a}合{poss[j]}{b}")
            if DIZHI_CHONG.get(a) == b:
                rel["chong_text"].append(f"{poss[i]}{a}冲{poss[j]}{b}")
            if DIZHI_HAI.get(a) == b:
                rel["hai_text"].append(f"{poss[i]}{a}害{poss[j]}{b}（{a}{b}穿）")

    # 动爻化出之爻 与 其余各爻 的六合/六冲/六害（化爻象事态走向引来的对象，
    # 如"二爻化出午火合五爻未土""二爻化出午火害初爻丑土（午丑穿）"）
    for i, l in enumerate(lines):
        if not l.get("is_moving"):
            continue
        hua_zhi = l.get("najia_bian", {}).get("dizhi", "?")
        if hua_zhi == "?":
            continue
        for j in range(len(lines)):
            if i == j:
                continue
            b = zhis[j]
            if b == "?":
                continue
            if DIZHI_HE.get(hua_zhi) == b:
                rel["he_text"].append(f"{poss[i]}化出{hua_zhi}合{poss[j]}{b}")
            if DIZHI_CHONG.get(hua_zhi) == b:
                rel["chong_text"].append(f"{poss[i]}化出{hua_zhi}冲{poss[j]}{b}")
            if DIZHI_HAI.get(hua_zhi) == b:
                rel["hai_text"].append(f"{poss[i]}化出{hua_zhi}害{poss[j]}{b}（{hua_zhi}{b}穿）")

    # 三刑
    zh_set = zhis
    for grp in XING_GROUPS:
        if len(grp) == 2:
            if grp[0] != grp[1]:
                # 子卯无礼之刑：子卯俱现才成刑
                idx = [i for i, z in enumerate(zh_set) if z in (grp[0], grp[1])]
                if len(idx) >= 2:
                    rel["xing_text"].append(
                        f"{'、'.join(f'{poss[k]}{zh_set[k]}' for k in idx)}构成{grp[0]}{grp[1]}无礼之刑")
            else:
                # 自刑：同支双现
                idx = [i for i, z in enumerate(zh_set) if z == grp[0]]
                if len(idx) >= 2:
                    rel["xing_text"].append(
                        f"{'、'.join(f'{poss[k]}{zh_set[k]}' for k in idx)}构成{grp[0]}自刑")
        else:
            # 寅巳申 / 丑戌未：出现组内任意两支即相刑
            idx = [i for i, z in enumerate(zh_set) if z in grp]
            if len(idx) >= 2:
                name = "无恩之刑" if grp[0] == "寅" else "恃势之刑"
                rel["xing_text"].append(
                    f"{'、'.join(f'{poss[k]}{zh_set[k]}' for k in idx)}构成{'、'.join(grp)}{name}")

    # 三合 / 半合：卦中六爻 + 月建 + 日辰 参合
    pool = {}
    for i, z in enumerate(zhis):
        if z != "?":
            pool.setdefault(z, []).append(poss[i])
    for label, z in (("月建", yuejian_zhi), ("日辰", richen_zhi)):
        if z:
            pool.setdefault(z, []).append(label)
    for trio, juzhi in SANHE_GROUPS.items():
        present = [z for z in trio if z in pool]
        if len(present) == 3:
            parts = []
            for z in trio:
                parts.append("、".join(f"{p}{z}" for p in pool[z]))
            rel["sanhe_text"].append(f"{'，'.join(parts)}构成三合{trio[0]}{trio[1]}{trio[2]}{juzhi}")
        elif len(present) == 2:
            parts = []
            for z in present:
                parts.append("、".join(f"{p}{z}" for p in pool[z]))
            rel["banhe_text"].append(f"{'，'.join(parts)}构成半合{'、'.join(present)}{juzhi}")

    # 月破 / 日破
    for i, z in enumerate(zhis):
        if z == "?":
            continue
        if DIZHI_CHONG.get(z) == yuejian_zhi:
            rel["po_text"].append(f"{poss[i]}{z}月破（月建{yuejian_zhi}冲）")
        if DIZHI_CHONG.get(z) == richen_zhi:
            rel["po_text"].append(f"{poss[i]}{z}日破（日辰{richen_zhi}冲）")

    return rel


def compute_line_interactions(lines) -> dict:
    """计算爻与爻之间的五行生克网络（供 AI 与前端展示）。
    逐爻列出：生我者/克我者/我生者/我克者/同气者；
    并提炼动爻↔世应、动爻↔动爻的要点关系。"""
    n = len(lines)
    rels = []
    for i in range(n):
        li = lines[i]
        wi = li.get("najia_ben", {}).get("wuxing", "?")
        sheng_wo, ke_wo, sheng_me, ke_me, tong = [], [], [], [], []
        for j in range(n):
            if i == j:
                continue
            wj = lines[j].get("najia_ben", {}).get("wuxing", "?")
            if wi == "?" or wj == "?":
                continue
            pos_j = lines[j].get("position", "")
            if wj == wi:
                tong.append(pos_j)
            elif SHENG.get(wi) == wj:
                sheng_me.append(pos_j)
            elif KE.get(wi) == wj:
                ke_me.append(pos_j)
            elif SHENG.get(wj) == wi:
                sheng_wo.append(pos_j)
            elif KE.get(wj) == wi:
                ke_wo.append(pos_j)
        rels.append({
            "position": li.get("position", ""),
            "wuxing": wi,
            "liuqin": li.get("liuqin_ben", "?"),
            "is_moving": li.get("is_moving", False),
            "sheng_wo": sheng_wo, "ke_wo": ke_wo,
            "sheng_me": sheng_me, "ke_me": ke_me,
            "tong": tong,
        })

    def rel_text(i, j):
        wi = lines[i].get("najia_ben", {}).get("wuxing", "?")
        wj = lines[j].get("najia_ben", {}).get("wuxing", "?")
        if wi == "?" or wj == "?":
            return ""
        pi, pj = lines[i].get("position", ""), lines[j].get("position", "")
        if SHENG.get(wi) == wj:
            return f"动爻{pi}{wi}生{lines[j].get('position','')}爻{wj}"
        if KE.get(wi) == wj:
            return f"动爻{pi}{wi}克{lines[j].get('position','')}爻{wj}"
        if SHENG.get(wj) == wi:
            return f"{lines[j].get('position','')}爻{wj}生动爻{pi}{wi}"
        if KE.get(wj) == wi:
            return f"{lines[j].get('position','')}爻{wj}克动爻{pi}{wi}"
        return f"动爻{pi}与{lines[j].get('position','')}爻同气{wi}"

    highlights = []
    geshan_highlights = []  # 动爻传导链
    moving = [i for i, l in enumerate(lines) if l.get("is_moving")]
    for a in range(len(moving)):
        for b in range(a + 1, len(moving)):
            t = rel_text(moving[a], moving[b])
            if t:
                highlights.append(t)
    shi_idx = next((i for i, l in enumerate(lines) if l.get("shi_ying_ben") == "世"), None)
    ying_idx = next((i for i, l in enumerate(lines) if l.get("shi_ying_ben") == "应"), None)
    for i in moving:
        for j, lab in ((shi_idx, "世爻"), (ying_idx, "应爻")):
            if j is None or i == j:
                continue
            t = rel_text(i, j)
            if t:
                highlights.append(t)

    # 动爻传导链：动爻A化出的六亲五行 = 动爻B本有的六亲五行 → A 传导至 B，A 最终化出 B 的变爻
    for idx_a in range(len(moving)):
        for idx_b in range(len(moving)):
            if idx_a == idx_b:
                continue
            i, j = moving[idx_a], moving[idx_b]
            lq_bian_i = lines[i].get("liuqin_bian", "?")
            dz_bian_i = lines[i].get("najia_bian", {}).get("dizhi", "")
            wx_bian_i = lines[i].get("najia_bian", {}).get("wuxing", "?")
            lq_ben_j = lines[j].get("liuqin_ben", "?")
            dz_ben_j = lines[j].get("najia_ben", {}).get("dizhi", "")
            wx_ben_j = lines[j].get("najia_ben", {}).get("wuxing", "?")
            if lq_bian_i == "?" or wx_bian_i == "?" or lq_ben_j == "?" or wx_ben_j == "?":
                continue
            if lq_bian_i == lq_ben_j and wx_bian_i == wx_ben_j:
                wx_bian_j = lines[j].get("najia_bian", {}).get("wuxing", "?")
                lq_bian_j = lines[j].get("liuqin_bian", "?")
                dz_bian_j = lines[j].get("najia_bian", {}).get("dizhi", "")
                pi = lines[i].get("position", "")
                pj = lines[j].get("position", "")
                lq_ben_i = lines[i].get("liuqin_ben", "?")
                dz_ben_i = lines[i].get("najia_ben", {}).get("dizhi", "")
                wx_ben_i = lines[i].get("najia_ben", {}).get("wuxing", "")
                msg = f"{lq_ben_i}{dz_ben_i}{wx_ben_i}{pi}动，化出{lq_bian_i}{dz_bian_i}{wx_bian_i}，即{pj}{lq_ben_j}{dz_ben_j}{wx_ben_j}，再传化{lq_bian_j}{dz_bian_j}{wx_bian_j}"
                geshan_highlights.append(msg)

    highlights = list(dict.fromkeys(highlights))
    geshan_highlights = list(dict.fromkeys(geshan_highlights))
    return {"lines": rels, "highlights": highlights, "geshan": geshan_highlights}


load_yao_ci()


# ====================================================================
# 模块九：AI 解卦引擎（OpenAI 兼容 API）
# ====================================================================

SYSTEM_PROMPT = """# Role
你是一位精通「六爻纳甲预测学」的实战派国学大师。你极其看重底层结构逻辑（五行生克制化），你的断卦过程必须严格遵循客观理气，严禁凭空发挥或过度依赖《周易》卦爻辞。

# Core Rules (绝对禁忌)
1. 数据绝对忠诚：必须 100% 使用输入参数中的地支、五行、六亲、六神、神煞。严禁凭空捏造排盘中未出现的任何爻象或变量（如捏造不存在的伏神或变爻）。
2. 现实基准至上：解卦必须受限于用户当前的真实状态。
3. 动爻取用法则（定用神枢机）：0动=静卦，以本卦彖辞(卦辞)取义；1动=取独发之爻；2动=取其中之阴爻，若同阴同阳则取上动之爻；3动=取居中之爻；4动=取下位之静爻；5动=取独静之爻；6动=乾卦用九、坤卦用六，其余参本卦彖辞(卦辞)与变卦爻辞合断。解卦必须以该「取用主爻」为枢机展开，其余动爻仅作佐证，切勿本末倒置。
4. 爻际生克网络至上：必须 100% 基于输入中已算好的「爻与爻五行生克网络」逐爻展开生克链条（谁生谁、谁克谁、谁与谁同气）。动爻→动爻、动爻→世应、动爻→取用主爻的作用路径必须逐条写明五行与方向（如：三爻动卯木生世爻巳火），静爻之间生克亦须纳入旺衰权衡。严禁只罗列爻位六神而不谈爻间生克。

# Workflow (标准操作程序 SOP)
你必须严格按照以下四个阶段进行思考和输出：

## 阶段一：客观脱水与现实定性 (Dehydration)
1. 剔除用户输入中的所有情绪化词汇（如"烦恼、针对、绝望"），提炼核心事件。
2. 【强制校验现实边界】：结合用户现状进行定性。例如测感情时，必须界定当前是"毫无现实交集/仅在网络群聊中偶尔互动"还是"已确立真实恋爱关系"。脱离现实基础的生克没有任何意义。

## 阶段二：动爻内部清算与真伪校验 (Internal Clearance & Time-Check)
【提取状态】：分析动爻前，强制复述："第X爻 [六亲][地支] 动，化 [变爻六亲][地支]"。
1. 第一层作用（化）：判断变爻对动爻的生克（化回头生/克/化进/退/空/破/绝）。
2. 【合冲刑害破核查】：对该动爻逐一检查「合冲刑害破关系」——是否月破/日破（无力或假破）、是否被三合/半合成局（力增）、是否被三刑（受制）、是否被六合绊住、六冲冲散或六害（穿）暗中受损，明写力量增减后进入下一阶段。
3. 【应期与真假校验】：若出现衰气（如化破、化绝），必须结合起卦的月建日辰推算应期。
   - 若衰败应期（如申月测，绝在寅月）距离当前较远，且用户现状完全有空间在应期到来前完成关系升华或事情终结，则此为"假绝/假破"，近期无碍。
   - 若事情必将跨越该应期，则为"真绝"。

## 阶段三：动爻外部作用链 (Action Chain)
若动爻在阶段二中清算存活（有力量），方可向外作用：
1. 目标锁定：动爻对世爻、应爻或核心用神是生还是克？
2. 贪生忘克：若多爻同动，必须优先寻找动爻之间的连环相生路径（如忌神发动，但原神亦动，则忌神贪生原神，原神生用神，化凶为吉）。
3. 动爻传导链：若输入中出现「动爻传导链」，说明前一个动爻化出的六亲五行恰好等于后一个动爻本有的六亲五行，形成连续传导。分析时须将传导链视为间接作用路径，标注传导方向与最终化出结果。
4. 合冲刑害破：若输入中出现「合冲刑害破关系」，必须纳入考量——六冲卦主速散、六合卦主迟和；爻逢月破日破则此爻失时无力，逢三合/半合则成局合力，逢三刑则多受制纠缠，逢六害（穿）则暗中受损、防不胜防。在推演中用神/忌神被合住、被冲破、被刑伤、被害穿时须明写对吉凶结论的影响。

## 阶段二·五：爻与爻生克网络核对 (Line-to-Line Interaction)
必须用输入中已算好的「爻与爻五行生克网络」逐爻核对，并写入输出：
1. 取用主爻的「生我/克我/我生/我克/同气」各为谁？受生则有力，受克则受制。
2. 动爻之间的生克方向（谁生谁、谁克谁、同气比和），多动时按贪生忘克理路走连环相生。
3. 动爻对世爻、应爻的生克路径必须明写五行与方向（如：三爻动卯木生世爻巳火，世爻得动助而旺）。
4. 【用神/忌神合冲刑害破核查】：对用神与忌神逐条应用「合冲刑害破关系」——是否被月破/日破、是否成三合/半合局、是否被三刑、是否被六合绊住、六冲冲散或六害（穿）暗中损伤，明确写出对旺衰与吉凶方向的影响（如：用神子水月破又逢丑土六合绊住，月内无力，须待午月冲开）。

## 阶段四：理象合一与最终输出 (Synthesis)
结合五行生克结论，最后引入相关的爻辞、神煞（如桃花、天乙贵人）与《焦氏易林》林辞作为细节补充，输出最终断语。爻辞与林辞必须采取「解读式引用」：各引原文一句（注明出处），随即解析其象意，并说明其与上述五行生克结论、世应用神关系的印证或呼应之处。严禁整段照抄经文，严禁只引不析。

## 合冲刑害破专项解读规则 (He-Chong-Xing-Hai-Po)
「合冲刑害破关系」已由系统按排盘数据算好并随本次输入给出，分析时必须逐条应用，不得忽略：
1. 六冲卦（本卦/变卦）：主事快、易散、多变、反复。测短期急事多应速；测长久之事（婚姻、合作、稳定状态）主不稳易散，须警惕虚而不实。
2. 六合卦：主事迟、和、缠绵、牵绊。测长久之事多主和合有利；测急事、竞争、脱困则主迟滞难成，须待冲开合局之日月。
3. 月破之爻：当月失时无力。动爻逢破则作用大减（假破），出月或逢合、逢值（填实）之日方可用；用神逢破而月内无救者事难成。日破之爻：当日无气，须待值日。
4. 三合局（含月建/日辰参合）：成局之爻力倍增，局中所化五行即事态主导方向。忌神成局则祸深难制，用神成局则福厚；成局遇冲开之日月应事。
5. 半合局：力量半成，须待第三支补全之日月方成全力。
6. 三刑：主是非口舌、官非诉讼、刑伤灾病、纠缠难解。用神临刑多受制受困；忌神临刑反可自相制约。
7. 爻间六合：用神被合则「绊住」，谋事难进，须待冲合之日月（如子丑合，午日冲开）；忌神被合则凶势暂缓。静爻逢合有被「合起」待动之象。
8. 爻间六冲：用神逢冲则「冲散」，事易中断或反复；忌神逢冲则祸去。先冲后合主先难后易，先合后冲主先成后败。
9. 爻间六害（六穿：子未、丑午、寅巳、卯辰、申亥、酉戌）：主暗中损害、口舌是非、防不胜防、关系疏离。用神逢害主暗中受制、明合暗害，事成也带瑕疵；忌神逢害则其凶势暗中受损。动爻化出之爻与某爻六害，象动后引来暗损。六害最宜取象（按地支五行、藏干、物象联想，五行生克仍以排盘数据为准，禁止自造不存在的爻）：
   - 丑午穿（午火本生丑土却相害，恩中藏怨、亲里带疏）：感情象貌合神离、暗藏暧昧、闷气冷战；事业象背地拆台、明升暗降、文书合同暗藏坑；财象财库被穿、暗耗漏财、被熟人算计；健康象心胃不和、湿火内伏、目翳昏暗；杂象牛马暗损、车马小摩擦、宅气晦暗。
   - 子未穿（水来土掩、暗流涌动）：象骨肉疏离、心绪与现实拉扯、田宅水患、计划被现实拖累。
   - 寅巳穿（木火相生反相害、恩将仇报）：象助长反噬、火旺焚木、肝胆心脑血管暗疾、口舌暗中酝酿。
   - 卯辰穿（木根入湿土）：象根基被暗中腐蚀、家宅不宁、草木枯根、肠胃湿滞。
   - 申亥穿（金生水反相害、泄气）：象暗中泄密、路上破财遭贼、肾水暗耗、刀兵水患。
   - 酉戌穿（口舌入火库）：象口舌是非暗起、酒色误事、自投罗网、肺胃燥热、合同陷阱。
   取象必须落在本次问事语境（感情/事业/财运/健康/出行等）中展开，不可空泛罗列。
10. 桃花（咸池）专项取象（子午卯酉为桃花，日支三合局查得并已标在爻上，动爻化出之爻的桃花也已标注）：爻临桃花主异性缘、情欲、风流露水之象。动爻化出之爻临桃花，代表此事引来的对象或事态走向带着情欲色彩；若化爻桃花又与应爻或用神相合（六合），即主动攀附之象，必须按化爻六亲断其动机：
   - 化出兄弟临桃花合应/用神：以情为饵、图财而来——兄弟主劫财耗财，断对方动机不纯，交往必有耗财破财，警惕骗财骗色（杀猪盘、酒托饭托、索要红包礼物）；
   - 化出妻财临桃花合应/用神：对方重利，情中带利，事成亦多财耗；
   - 化出官鬼临桃花合应/用神：对方纠缠难断、或有主之身（已婚/有伴）暗度陈仓，易惹口舌官非；
   - 化出子孙/父母临桃花合应/用神：逢场作戏居多，玩心大于真心；
   - 桃花合应爻而应爻被合绊：对方心被情欲牵绊、三心二意，事多虚应。
   凡遇「兄弟+桃花+合」组合，断语必须明写破财方向与防骗提示。
11. 墓库·穿财源·财生官 串联取象（实战总结；五行生克一律以排盘数据为准，禁止自造不存在的爻）：
   - 妻财入墓（金墓丑、木墓未、火墓戌、水墓辰，以日辰/月建及卦中同五行之库为墓）：财爻入墓 → 财与女人被藏匿、套牢、暗昧不明；多财同入一墓 → 两处钱或两个对象皆在暗中；财旺成局（三合）又入墓 → 钱多却套牢、明暗账混杂。
   - 子孙为财源（子孙五行生财）：兄弟/忌神六害穿子孙（如午丑穿）→ 明面合人热络、暗地啃蚀财源，断「合是幌子、穿是里子」，事外热内耗、表面风光暗中漏财。
   - 财生官（财爻五行生官鬼）：财往官上贴 → 女人或钱财最终流向官鬼所象（背后男人/掌权者/官方渠道），断「上头还有人、钱落他手」。
   - 桃花或合局之合爻逢冲（被日辰、月建或他爻相冲，如午未合遇丑冲未）：合不牢、虚合，主事散。
   - 人物定位：测他人/群友时，兄弟可象同辈朋友，应爻常为所测之人或对方，化爻象事态走向与引来的对象；须按问事语境串联上述关系展开断语。
必须在【动爻枢机推演】与【世应与用神解析】中，对每个动爻及用神/忌神明写：是否被合/被冲/被刑/被害/被破/成局，以及由此对力量与吉凶方向的增减。

# Output Format
请严格按以下结构输出给用户：

【客观局势重构】
（剔除用户主观情绪，客观描述当前事态本质及现实阶段定性。以卦象的客观生克结构校准用户认知偏差。）

【动爻枢机推演】
（展示核心变量的推导。如果是静卦，则改为【静卦态势分析】。必须用箭头 `->` 画出力量传导路径，如：变爻 -> 生/克 -> 动爻 -> 生/克 -> 世爻。严禁引入排盘中不存在的爻。）

【世应与用神解析】
（结合日月建，定世爻与用神的旺衰，剖析事情成败的主客体关系；逐项写明用神/忌神是否被合住、冲散、刑伤、害穿、冲破或成三合半合局，及其对成败的影响。）

【细节象意与实战建议】
（先对取用主爻爻辞、动爻爻辞与《焦氏易林》林辞逐条解读：引原文一句、解析象意、印证卦理；再结合神煞、六神，给出最符合当前现实阶段的务实建议，指出应期时间窗口。）

---

请用半文半白的优雅中文风格回复，推导过程必须写明五行生克关系。爻辞与《焦氏易林》林辞必须解读式引用（引一句原文并解析象意、印证卦理），严禁整段照抄、严禁只引不析。所有分析必须 100% 基于排盘数据。全篇务求精炼克制、删繁就简，总字数控制在 1200 字以内，但必须完整走完四个板块并写出最终断语收尾，严禁因篇幅问题中途截断、半途而废。"""


def build_user_prompt(
    question: str,
    time_info: dict,
    ben_gua_name: str,
    bian_gua_name: str,
    dong_yao_labels: list,
    ben_gua_code: str,
    bian_gua_code: str,
    lines: list,
    jiao_yilin: str = "",
) -> str:
    """构建提交给 AI 的完整 User Prompt"""
    dong_yao_str = "、".join(dong_yao_labels) if dong_yao_labels else "无动爻（静卦）"

    # 组装六神+六亲+纳甲+世应+神煞 全量信息
    liushen_parts = []
    for line in lines:
        shi_ying = line.get('shi_ying_ben', '')
        sy_tag = f"【{shi_ying}爻】" if shi_ying else ""
        # 神煞标签
        shensha_list = line.get('shensha', [])
        ss_str = f"（临{','.join(shensha_list)}）" if shensha_list else ""
        # 动爻：附加化出之爻的六亲/地支/神煞（含桃花），供合冲刑害破与取象判断
        hua_str = ""
        if line['is_moving']:
            hua_ss = line.get('shensha_bian', [])
            hua_ss_str = f"（临{','.join(hua_ss)}）" if hua_ss else ""
            hua_str = (f"，化出{line.get('liuqin_bian', '？')}"
                       f"{line.get('najia_bian', {}).get('dizhi', '？')}"
                       f"{line.get('najia_bian', {}).get('wuxing', '？')}"
                       f"{hua_ss_str}")
        liushen_parts.append(
            f"{line['position']}{sy_tag}：临【{line.get('liu_shen', '？')}】"
            f"，{line.get('liuqin_ben', '？')}"
            f"，{line.get('najia_ben', {}).get('dizhi', '？')}"
            f"{line.get('najia_ben', {}).get('wuxing', '？')}"
            f"{'（动）' if line['is_moving'] else '（静）'}"
            f"{ss_str}{hua_str}"
        )
    liushen_str = "；".join(liushen_parts)

    # 单独列出世爻和应爻
    shi_yao_info = ""
    ying_yao_info = ""
    for line in lines:
        sy = line.get('shi_ying_ben', '')
        if sy == '世':
            shi_yao_info = (
                f"世爻位于{line['position']}，临{line.get('liu_shen','？')}，"
                f"六亲为{line.get('liuqin_ben','？')}，"
                f"地支{line.get('najia_ben',{}).get('dizhi','？')}"
                f"{line.get('najia_ben',{}).get('wuxing','？')}"
                f"{'，此为动爻' if line['is_moving'] else '，静爻'}"
            )
        elif sy == '应':
            ying_yao_info = (
                f"应爻位于{line['position']}，临{line.get('liu_shen','？')}，"
                f"六亲为{line.get('liuqin_ben','？')}，"
                f"地支{line.get('najia_ben',{}).get('dizhi','？')}"
                f"{line.get('najia_ben',{}).get('wuxing','？')}"
                f"{'，此为动爻' if line['is_moving'] else '，静爻'}"
            )

    # 动爻取用法则定主 + 附本卦/变卦爻辞
    yaoci = compute_yao_info(lines, ben_gua_name, bian_gua_name)
    yao_parts = [f"动爻取用：{yaoci['zhugong_rule']}"]
    if yaoci['zhugong_pos']:
        yao_parts.append(f"　取用主爻：{yaoci['zhugong_pos']}，其爻辞『{yaoci['zhugong_ci']}』")
    if yaoci['special']:
        yao_parts.append(f"　{yaoci['special']}")
    if yaoci['ben_dong_yao_ci']:
        yao_parts.append("　本卦动爻爻辞：" + "；".join(yaoci['ben_dong_yao_ci']))
    else:
        yao_parts.append("　本卦为静卦，无动爻爻辞可引，以彖辞（卦辞）取象。")
    if yaoci['bian_dong_yao_ci']:
        yao_parts.append("　变卦对应动爻爻辞：" + "；".join(yaoci['bian_dong_yao_ci']))
    yaoci_text = "\n".join(yao_parts)

    # 爻与爻五行生克网络
    inter = compute_line_interactions(lines)
    inter_lines = []
    for r in inter["lines"]:
        tag = "动" if r["is_moving"] else "静"
        rel = []
        if r["sheng_wo"]: rel.append("生我：" + "、".join(r["sheng_wo"]))
        if r["ke_wo"]: rel.append("克我：" + "、".join(r["ke_wo"]))
        if r["sheng_me"]: rel.append("我生：" + "、".join(r["sheng_me"]))
        if r["ke_me"]: rel.append("我克：" + "、".join(r["ke_me"]))
        if r["tong"]: rel.append("同气：" + "、".join(r["tong"]))
        rel_str = "，".join(rel) if rel else "无直接生克"
        inter_lines.append(f"{r['position']}（{r['wuxing']}·{r['liuqin']}·{tag}）→ {rel_str}")
    inter_text = "\n".join(inter_lines)
    hl_text = "；".join(inter["highlights"]) if inter["highlights"] else "动爻与世应、动爻之间无直接五行生克"
    geshan_text = "；".join(inter["geshan"]) if inter["geshan"] else "无动爻传导链"

    # 合冲刑破关系
    guanxi = compute_gua_relations(
        lines,
        yuejian_zhi=time_info["yuejian"][1],
        richen_zhi=time_info["richen"][1],
        bian_code=bian_gua_code,
        bian_zhis=[l.get("najia_bian", {}).get("dizhi", "?") for l in lines],
    )
    gx_parts = []
    if guanxi["liu_chong"]:
        gx_parts.append("本卦为六冲卦（六爻地支两两相冲，主事快而散）")
    if guanxi["liu_he"]:
        gx_parts.append("本卦为六合卦（六爻地支两两相合，主事迟而和）")
    if guanxi["bian_liu_chong"]:
        gx_parts.append(f"变卦{bian_gua_name}亦为六冲卦（六爻地支两两相冲，主事后变而散）")
    if guanxi["bian_liu_he"]:
        gx_parts.append(f"变卦{bian_gua_name}亦为六合卦（六爻阴阳两两相应，主事后和而迟）")
    if guanxi["he_text"]:
        gx_parts.append("爻间六合：" + "、".join(guanxi["he_text"]))
    if guanxi["chong_text"]:
        gx_parts.append("爻间六冲：" + "、".join(guanxi["chong_text"]))
    if guanxi["hai_text"]:
        gx_parts.append("爻间六害（穿）：" + "、".join(guanxi["hai_text"]))
    if guanxi["xing_text"]:
        gx_parts.append("三刑：" + "；".join(guanxi["xing_text"]))
    if guanxi["sanhe_text"]:
        gx_parts.append("三合：" + "；".join(guanxi["sanhe_text"]))
    if guanxi["banhe_text"]:
        gx_parts.append("半合：" + "；".join(guanxi["banhe_text"]))
    if guanxi["po_text"]:
        gx_parts.append("月破/日破：" + "；".join(guanxi["po_text"]))
    guanxi_text = "\n".join(gx_parts) if gx_parts else "卦中无六冲六合、三刑、三合半合、月破日破等特殊关系"

    return f"""用户问题：{question}

起卦时间：{time_info['solar_date']}（{time_info['lunar_date']}）
月建：{time_info['yuejian']}，日辰：{time_info['richen']}，旬空：{time_info['xunkong']}

本卦：{ben_gua_name}（卦码：{ben_gua_code}），变卦：{bian_gua_name}（卦码：{bian_gua_code}）
动爻位置：{dong_yao_str}

世应定位：
　{shi_yao_info}
　{ying_yao_info}

各爻全量排盘（从下到上）：
{liushen_str}

{yaoci_text}

爻与爻五行生克网络（下至上，已按各爻五行算好）：
{inter_text}
动爻↔世应/动爻要点：{hl_text}
动爻传导链：{geshan_text}
合冲刑害破关系：
{guanxi_text}

{gua_jiao_yilin_text(ben_gua_name, bian_gua_name, jiao_yilin)}
请严格按照 SOP 工作流执行：先脱水提炼核心事实并校验现实边界；再确定取用主爻并对其施行动爻内部清算（判断化回头生克真伪）；然后分析动爻对外作用链（生克世应用神）；最后在【细节象意与实战建议】板块中逐条解读爻辞与焦氏易林林辞（各引原文一句，解析象意，印证卦理）并输出断语。必须按【客观局势重构】【动爻枢机推演】（静卦为【静卦态势分析】）【世应与用神解析】【细节象意与实战建议】四个板块输出。严禁引入排盘中不存在的爻象，所有推导必须写明五行生克过程。"""


def gua_jiao_yilin_text(ben_gua_name: str, bian_gua_name: str, jiao_yilin: str) -> str:
    """构造焦氏易林林辞提示片段；若无匹配则降级为常规爻辞指引"""
    if jiao_yilin:
        return (
            f"《焦氏易林》『{full_to_short(ben_gua_name)}之{full_to_short(bian_gua_name)}』"
            f"林辞：{jiao_yilin}\n"
            "（此林辞为古人断此本变卦组合的权威象占参考，必须引原文一句并解析象意、印证卦理，但严禁整段照抄或当作生克依据。）\n"
        )
    return "（此本变卦组合未录入《焦氏易林》，可仅引用本卦卦辞与本题爻辞。\n"


async def ai_interpret(question: str, time_info: dict, gua_result: dict) -> str:
    """
    调用 OpenAI 兼容 API 进行 AI 解卦。
    如果未配置 API，返回模拟解卦结果。
    如果 API 调用失败，通过 HTTPException 返回结构化错误信息。
    """
    try:
        ben_gua = lookup_gua(gua_result["ben_gua_code"])
        bian_gua = lookup_gua(gua_result["bian_gua_code"])
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"卦码查找失败：gua_result 数据结构异常，ben_gua_code/bian_gua_code 缺失。原因：{str(e)}")

    if not OPENAI_API_KEY or not OPENAI_BASE_URL:
        raise HTTPException(
            status_code=503,
            detail="AI 解卦引擎未配置：请在项目根目录创建 .env 文件，填入 OPENAI_BASE_URL 和 OPENAI_API_KEY。"
                   "可参考 .env.example 模板。"
        )

    try:
        jiao_yilin = lookup_jiao(ben_gua["name"], bian_gua["name"])
        user_prompt = build_user_prompt(
            question, time_info,
            ben_gua["name"], bian_gua["name"],
            gua_result["dong_yao_labels"],
            gua_result["ben_gua_code"], gua_result["bian_gua_code"],
            gua_result["lines"],
            jiao_yilin=jiao_yilin,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Prompt 组装失败。原因：{str(e)}")

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 2048,
                },
            )

            if resp.status_code == 401:
                raise HTTPException(status_code=502, detail="AI API 认证失败：API Key 无效或已过期，请检查 .env 中的 OPENAI_API_KEY")
            elif resp.status_code == 429:
                raise HTTPException(status_code=502, detail="AI API 请求过频或额度不足，请稍后再试或检查账户余额")
            elif resp.status_code == 404:
                raise HTTPException(status_code=502, detail=f"AI API 端点未找到：请检查 .env 中的 OPENAI_BASE_URL 和 OPENAI_MODEL（当前 model={OPENAI_MODEL}，url={OPENAI_BASE_URL}）")
            elif resp.status_code >= 500:
                raise HTTPException(status_code=502, detail=f"AI API 服务器内部错误（状态码 {resp.status_code}），请稍后重试。响应：{resp.text[:300]}")
            elif resp.status_code != 200:
                error_body = resp.text[:500]
                raise HTTPException(status_code=502, detail=f"AI API 返回异常（状态码 {resp.status_code}）：{error_body}")

            data = resp.json()
            return data["choices"][0]["message"]["content"]

    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="AI API 连接失败：无法连接到 " + OPENAI_BASE_URL + "，请检查网络或 BASE_URL 是否正确")
    except httpx.ReadTimeout:
        raise HTTPException(status_code=502, detail="AI API 请求超时（90 秒）：大模型响应太慢，请重试或更换模型")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"AI API 网络请求异常：{type(e).__name__} - {str(e)}")




# ====================================================================
# API 路由
# ====================================================================

class DivinationRequest(BaseModel):
    question: str = Field(default="", max_length=2000)   # 限长，防止超长文本拖垮 prompt / 放大 API 费用
    gua_result: dict
    time_info: dict


class DivinationResponse(BaseModel):
    time_info: dict
    gua_result: dict
    ben_gua_info: dict
    bian_gua_info: dict
    interpretation: str
    jiao_yilin: str = ""


@app.post("/api/divine", response_model=DivinationResponse)
async def divine(req: DivinationRequest):
    """AI 解卦接口 —— 严格使用前端传入的排盘数据，严禁二次随机"""
    time_info = req.time_info
    gua_result = req.gua_result

    # 参数校验：卦码必须存在于知识库、lines 必须为 6 爻，防止无效/伪造数据流入 AI prompt
    ben_code = str(gua_result.get("ben_gua_code", ""))
    bian_code = str(gua_result.get("bian_gua_code", ""))
    lines_in = gua_result.get("lines")
    if (ben_code not in HEXAGRAMS_DB or bian_code not in HEXAGRAMS_DB
            or not isinstance(lines_in, list) or len(lines_in) != 6):
        raise HTTPException(status_code=422, detail="排盘数据无效：卦码须为六十四卦且 lines 须为 6 爻")

    # 爻象自洽校验：每爻阴阳/动静必须与本卦、变卦卦码一致，防止伪造爻象流入 AI prompt
    try:
        for i, line in enumerate(lines_in):
            if not isinstance(line, dict):
                raise ValueError
            if int(line.get("yin_yang")) != int(ben_code[i]):
                raise ValueError
            if bool(line.get("is_moving")) != (ben_code[i] != bian_code[i]):
                raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="排盘数据无效：爻象与卦码不一致")

    # 动爻标签以服务端卦码为准，不采用前端传入值
    gua_result["dong_yao_labels"] = [
        YAO_POSITIONS[i] for i, line in enumerate(lines_in) if line["is_moving"]
    ]

    # 时间信息关键字段校验，避免缺字段导致 Prompt 组装异常（500）
    required_ti = ("solar_date", "lunar_date", "yuejian", "richen", "xunkong")
    if not all(isinstance(time_info.get(k), str) and time_info.get(k) for k in required_ti):
        raise HTTPException(status_code=422, detail="时间信息无效：缺少起卦时间/月建/日辰等字段")
    if len(time_info["yuejian"]) != 2 or len(time_info["richen"]) != 2:
        raise HTTPException(status_code=422, detail="时间信息无效：月建/日辰须为干支两字")

    # 查找卦象
    ben_gua_info = lookup_gua(ben_code)
    bian_gua_info = lookup_gua(bian_code)

    # 焦氏易林林辞（供前端展示 + AI 引用）
    jiao_yilin = lookup_jiao(ben_gua_info["name"], bian_gua_info["name"])

    # AI 解卦
    interpretation = await ai_interpret(
        req.question or "未说明具体事项", time_info, gua_result
    )

    return DivinationResponse(
        time_info=time_info,
        gua_result=gua_result,
        ben_gua_info=ben_gua_info,
        bian_gua_info=bian_gua_info,
        interpretation=interpretation,
        jiao_yilin=jiao_yilin,
    )


def _enrich_paipan(gua_result: dict, now: Optional[datetime] = None) -> dict:
    """根据 gua_result(本卦/变卦码+基础 lines) 注入六神/纳甲/六亲/世应/神煞。
    返回 {time_info, gua_result, ben_gua_info, bian_gua_info}，供排盘接口复用。
    now 为可指定的起卦时间（如手动填写的历史时间）；None 则取服务器当前时间。"""
    time_info = get_ganzhi_info(now)

    # 六神
    liushen_list = assign_liushen(time_info["rigan"])
    for i, line in enumerate(gua_result["lines"]):
        line["liu_shen"] = liushen_list[i]

    # 纳甲（本卦+变卦）
    najia_ben = assign_najia(gua_result["ben_gua_code"])
    najia_bian = assign_najia(gua_result["bian_gua_code"])

    # 六亲（变卦六亲以本卦宫五行为基准：变爻不离本宫）
    ben_gua_info = lookup_gua(gua_result["ben_gua_code"])
    ben_palace_element = ben_gua_info.get("element", "?")
    liuqin_ben = assign_liuqin_for_hexagram(gua_result["ben_gua_code"], najia_ben)
    liuqin_bian = assign_liuqin_for_hexagram(gua_result["bian_gua_code"], najia_bian,
                                             palace_element_override=ben_palace_element)
    for i, line in enumerate(gua_result["lines"]):
        line["najia_ben"] = najia_ben[i]
        line["najia_bian"] = najia_bian[i]
        line["liuqin_ben"] = liuqin_ben[i]
        line["liuqin_bian"] = liuqin_bian[i]

    # 世应
    shiying_ben = assign_shiying(gua_result["ben_gua_code"])
    shiying_bian = assign_shiying(gua_result["bian_gua_code"])
    for i, line in enumerate(gua_result["lines"]):
        line["shi_ying_ben"] = shiying_ben[i]
        line["shi_ying_bian"] = shiying_bian[i]

    # 神煞（日干日支 + 本卦纳甲地支）
    ri_gan = time_info["rigan"]
    ri_zhi = time_info["richen"][1]
    for line in gua_result["lines"]:
        line["shensha"] = calculate_shensha(ri_gan, ri_zhi, line["najia_ben"]["dizhi"])
        # 动爻同时标注化出之爻的神煞（如桃花），供 AI 判断化爻的情欲属性
        line["shensha_bian"] = (
            calculate_shensha(ri_gan, ri_zhi, line["najia_bian"]["dizhi"])
            if line["is_moving"] else []
        )

    bian_gua_info = lookup_gua(gua_result["bian_gua_code"])

    # 动爻取用主爻 + 爻辞（供前端展示）
    gua_result["yao_info"] = compute_yao_info(
        gua_result["lines"], ben_gua_info["name"], bian_gua_info["name"]
    )

    # 爻与爻五行生克网络（供前端展示与 AI 解卦）
    gua_result["interactions"] = compute_line_interactions(gua_result["lines"])

    # 合冲刑破关系（六冲/六合/三刑/三合/半合/月破日破；变卦六冲/六合）
    gua_result["guanxi"] = compute_gua_relations(
        gua_result["lines"],
        yuejian_zhi=time_info["yuejian"][1],
        richen_zhi=time_info["richen"][1],
        bian_code=gua_result["bian_gua_code"],
        bian_zhis=[l.get("najia_bian", {}).get("dizhi", "?") for l in gua_result["lines"]],
    )

    return {
        "time_info": time_info,
        "gua_result": gua_result,
        "ben_gua_info": ben_gua_info,
        "bian_gua_info": bian_gua_info,
    }


class PaipanRequest(BaseModel):
    ben_gua_code: str = ""
    bian_gua_code: str = ""
    qigua_time: Optional[str] = None   # 起卦时间（datetime-local 格式），缺省取服务器当前时间


@app.post("/api/paipan")
async def paipan(req: PaipanRequest):
    """
    手动指定本卦/变卦进行排盘（无需随机起卦）。
    动爻 = 两卦码逐爻差异；返回完整排盘结构。
    """
    ben = req.ben_gua_code
    bian = req.bian_gua_code
    if len(ben) != 6 or len(bian) != 6 or ben not in HEXAGRAMS_DB or bian not in HEXAGRAMS_DB:
        raise HTTPException(status_code=422, detail="请提供有效的 6 位本卦与变卦卦码（0/1，从下到上）")

    lines = []
    for i in range(6):
        yt = int(ben[i])
        moving = ben[i] != bian[i]
        toss_sum = 9 if (moving and yt == 1) else (6 if moving else (7 if yt == 1 else 8))
        info = LINE_INFO[toss_sum]
        lines.append({
            "position": YAO_POSITIONS[i],
            "position_index": i,
            "toss_sum": toss_sum,
            "name": info["name"],
            "yin_yang": info["yin_yang"],
            "is_moving": info["is_moving"],
            "symbol": info["symbol"],
            "moving_mark": info["moving_mark"],
            "display": (f"{info['symbol']} {info['moving_mark']}".strip()
                        if info["is_moving"] else info["symbol"]),
        })

    dong_yao_indices = [i for i, d in enumerate(lines) if d["is_moving"]]
    gua_result = {
        "lines": lines,
        "ben_gua_code": ben,
        "bian_gua_code": bian,
        "dong_yao_indices": dong_yao_indices,
        "dong_yao_labels": [YAO_POSITIONS[i] for i in dong_yao_indices],
    }
    return _enrich_paipan(gua_result, now=parse_qigua_time(req.qigua_time))


@app.get("/api/hexagrams")
async def hexagrams_list():
    """返回六十四卦知识库，供前端构建卦选择器"""
    return HEXAGRAMS_DB


@app.get("/api/health")
async def health():
    """健康检查 + 配置状态"""
    return {
        "status": "ok",
        "hexagrams_loaded": len(HEXAGRAMS_DB),
        "ai_configured": bool(OPENAI_API_KEY and OPENAI_BASE_URL),
        "ai_model": OPENAI_MODEL,
        "ai_base_url": (OPENAI_BASE_URL[:40] + "...") if len(OPENAI_BASE_URL) > 40 else OPENAI_BASE_URL,
    }


# ── 静态文件 & 首页 ──
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    resp = FileResponse(str(static_dir / "index.html"))
    # 强制不缓存首页，避免浏览器沿用旧版 index.html 导致功能看不到
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
