/**
 * captionText.ts —— 画面内字幕的折行 / 字数测量（纯函数，不依赖 React / Remotion）。
 *
 * 为什么单独一个文件：折行规则必须能被 esbuild 单独打包做断言测试，也要能像
 * `scripts/subtitles.py` 一样有可回归的测试入口。画面内 <Caption> 与 ASS 硬字幕
 * 必须是**同一套折行算法** —— 否则同一句旁白，画面版和烧入版会断在不同的字上。
 *
 * 算法与 `scripts/subtitles.py` 的 `_fold_text()` 逐条对应（R5-TPL-02）：
 *   1) 折行单位是**原子**：CJK / 标点 / 空格逐字，拉丁词、URL、数字串整段不可拆；
 *   2) 贪心装到 `maxChars` 个字，再往前找**最后一个合法切点**；
 *   3) 避头尾（禁则）：收尾标点（。，、！？；：…— 等）不能放行首，
 *      起始标点（（「『《【 等）不能放行尾；
 *   4) 平衡：两行差不多长比「满行 + 短尾巴」好看；平衡点附近 ±2 字优先找标点边界，
 *      所以「小票」这类中文词不会为了凑标点被拆开；
 *   5) 第二行不能以「单个字 + 收尾标点」开头（那是中文词被拆开的指纹，
 *      例如旧版把「十几分钟」切成了「…十几 / 分钟？」）；
 *   6) 找不到合法切点时，宁可首行多含 1~2 个收尾标点（最多 2 个字），
 *      也不把标点切到行首（`FOLD_OVERFLOW_GRACE`）。
 *
 * 字数是「中文正文字数」：东亚洲宽字符算 1 个字、半角算 0.5，四舍五入进位 ——
 * `captions.maxChars` 按这个口径理解（与 subtitles.py 的 `_vis_len` 一致）。
 */

/** 收尾标点：不能出现在行首。与 subtitles.py 的 CLOSING_PUNCT 逐字一致。 */
export const CLOSING_PUNCT = "。，、！？；：…—,.!?;:）〕」』》〉】”’%";
/** 起始标点：不能出现在行尾（左括号 / 左引号后面必须还有字）。 */
export const OPENING_PUNCT = "（〔「『《〈【“‘(";
/** 允许「为避免行首标点」而超出 maxChars 的宽容度（字）。 */
export const FOLD_OVERFLOW_GRACE = 2;
/** 平衡折行时在平衡点附近找合法切点的搜索半径（字）。 */
const BALANCE_LOOKBACK = 2;

/**
 * C0/C1 控制字符（Python `subtitles.py::CONTROL_CHARS_RE` 逐字一致）。
 * 不含 `\t`(0x09) / `\n`(0x0a) / `\r`(0x0d) —— 它们交给空白压缩。
 */
const CONTROL_CHARS_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/g;
/**
 * Python `re` 里 `\s` 的等价字符集（Unicode 模式）。
 * ❗不能直接用 JS 的 `\s`：JS 的 `\s` 包含 `U+FEFF`，Python 的不包含 ——
 * 直接用会把文案里的 BOM 当成空格吞掉，而 Python 侧把它当 0.5 字宽的字符计入折行。
 */
const PY_SPACE_CLASS = " \\t\\n\\r\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const PY_SPACE_RUN_RE = new RegExp(`[${PY_SPACE_CLASS}]+`, "g");
const PY_SPACE_TRIM_RE = new RegExp(`^[${PY_SPACE_CLASS}]+|[${PY_SPACE_CLASS}]+$`, "g");
const PY_SPACE_TRIM_END_RE = new RegExp(`[${PY_SPACE_CLASS}]+$`, "g");

/** Python `str.strip()` 口径（JS 的 `String.trim()` 会把 U+FEFF 也剥掉，R7-TS-02）。 */
const pyStrip = (text: string): string => text.replace(PY_SPACE_TRIM_RE, "");

/** Python `str.rstrip()` 口径。 */
const pyTrimEnd = (text: string): string => text.replace(PY_SPACE_TRIM_END_RE, "");

/**
 * 「已分配且 EAW=W/F」的码点区段表 —— `subtitles.py::_vis_len` 用的判定口径的逐段镜像。
 * Python 侧是 `unicodedata.east_asian_width(ch) in ("W","F")`，这里把同样的码点合并成
 * 128 个区段写死，两边必须同口径（AGENTS.md 阶段 7 要求字幕折行两侧一致，R7-TS-01）。
 *
 * 为什么不继续用宽泛区间近似：EAW 不是连续区段。例如 `✅`(U+2705)、`⭐`(U+2B50)、
 * `❗`(U+2757) 是 W，但同一个 emoji 平面里另有一半是 N；国旗（区域指示符
 * U+1F1E6–U+1F1FF）也是 N（两面旗合计 1 个字宽）。以前用 `0x1F000–0x1FAFF` 整段近似，
 * 结果画面字幕把 `✅` 算成 0 个宽（四舍五入到 0）、把 `🇨🇳` 算成 2 个字宽，与烧入的
 * ASS 折行对不上（真实文案里 ✅ / ❌ / 🔥 / 🇨🇳 并不罕见）。
 *
 * 表由 Python 3.12（Unicode 15.0）的 unicodedata 导出，按码点升序排列（二分查找）。
 * `…`(U+2026)、`—`(U+2014) 是 A（ambiguous），表里没有、按 0.5 算，与 `_vis_len` 一致。
 * 升级 Python 大版本后若出现新的 W 区段，用 `scripts/subtitles.py` 的口径重新导出补进来。
 */
const EAW_WIDE_RANGES: readonly (readonly [number, number])[] = [
  [0x1100, 0x115f], [0x231a, 0x231b], [0x2329, 0x232a], [0x23e9, 0x23ec],
  [0x23f0, 0x23f0], [0x23f3, 0x23f3], [0x25fd, 0x25fe], [0x2614, 0x2615],
  [0x2648, 0x2653], [0x267f, 0x267f], [0x2693, 0x2693], [0x26a1, 0x26a1],
  [0x26aa, 0x26ab], [0x26bd, 0x26be], [0x26c4, 0x26c5], [0x26ce, 0x26ce],
  [0x26d4, 0x26d4], [0x26ea, 0x26ea], [0x26f2, 0x26f3], [0x26f5, 0x26f5],
  [0x26fa, 0x26fa], [0x26fd, 0x26fd], [0x2705, 0x2705], [0x270a, 0x270b],
  [0x2728, 0x2728], [0x274c, 0x274c], [0x274e, 0x274e], [0x2753, 0x2755],
  [0x2757, 0x2757], [0x2795, 0x2797], [0x27b0, 0x27b0], [0x27bf, 0x27bf],
  [0x2b1b, 0x2b1c], [0x2b50, 0x2b50], [0x2b55, 0x2b55], [0x2e80, 0x2e99],
  [0x2e9b, 0x2ef3], [0x2f00, 0x2fd5], [0x2ff0, 0x2ffb], [0x3000, 0x303e],
  [0x3041, 0x3096], [0x3099, 0x30ff], [0x3105, 0x312f], [0x3131, 0x318e],
  [0x3190, 0x31e3], [0x31f0, 0x321e], [0x3220, 0x3247], [0x3250, 0x4dbf],
  [0x4e00, 0xa48c], [0xa490, 0xa4c6], [0xa960, 0xa97c], [0xac00, 0xd7a3],
  [0xf900, 0xfa6d], [0xfa70, 0xfad9], [0xfe10, 0xfe19], [0xfe30, 0xfe52],
  [0xfe54, 0xfe66], [0xfe68, 0xfe6b], [0xff01, 0xff60], [0xffe0, 0xffe6],
  [0x16fe0, 0x16fe4], [0x16ff0, 0x16ff1], [0x17000, 0x187f7], [0x18800, 0x18cd5],
  [0x18d00, 0x18d08], [0x1aff0, 0x1aff3], [0x1aff5, 0x1affb], [0x1affd, 0x1affe],
  [0x1b000, 0x1b122], [0x1b132, 0x1b132], [0x1b150, 0x1b152], [0x1b155, 0x1b155],
  [0x1b164, 0x1b167], [0x1b170, 0x1b2fb], [0x1f004, 0x1f004], [0x1f0cf, 0x1f0cf],
  [0x1f18e, 0x1f18e], [0x1f191, 0x1f19a], [0x1f200, 0x1f202], [0x1f210, 0x1f23b],
  [0x1f240, 0x1f248], [0x1f250, 0x1f251], [0x1f260, 0x1f265], [0x1f300, 0x1f320],
  [0x1f32d, 0x1f335], [0x1f337, 0x1f37c], [0x1f37e, 0x1f393], [0x1f3a0, 0x1f3ca],
  [0x1f3cf, 0x1f3d3], [0x1f3e0, 0x1f3f0], [0x1f3f4, 0x1f3f4], [0x1f3f8, 0x1f43e],
  [0x1f440, 0x1f440], [0x1f442, 0x1f4fc], [0x1f4ff, 0x1f53d], [0x1f54b, 0x1f54e],
  [0x1f550, 0x1f567], [0x1f57a, 0x1f57a], [0x1f595, 0x1f596], [0x1f5a4, 0x1f5a4],
  [0x1f5fb, 0x1f64f], [0x1f680, 0x1f6c5], [0x1f6cc, 0x1f6cc], [0x1f6d0, 0x1f6d2],
  [0x1f6d5, 0x1f6d7], [0x1f6dc, 0x1f6df], [0x1f6eb, 0x1f6ec], [0x1f6f4, 0x1f6fc],
  [0x1f7e0, 0x1f7eb], [0x1f7f0, 0x1f7f0], [0x1f90c, 0x1f93a], [0x1f93c, 0x1f945],
  [0x1f947, 0x1f9ff], [0x1fa70, 0x1fa7c], [0x1fa80, 0x1fa88], [0x1fa90, 0x1fabd],
  [0x1fabf, 0x1fac5], [0x1face, 0x1fadb], [0x1fae0, 0x1fae8], [0x1faf0, 0x1faf8],
  [0x20000, 0x2a6df], [0x2a700, 0x2b739], [0x2b740, 0x2b81d], [0x2b820, 0x2cea1],
  [0x2ceb0, 0x2ebe0], [0x2f800, 0x2fa1d], [0x30000, 0x3134a], [0x31350, 0x323af],
];

export const isWideChar = (ch: string): boolean => {
  const cp = ch.codePointAt(0) ?? 0;
  let low = 0;
  let high = EAW_WIDE_RANGES.length - 1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    const [start, end] = EAW_WIDE_RANGES[mid];
    if (cp < start) high = mid - 1;
    else if (cp > end) low = mid + 1;
    else return true;
  }
  return false;
};

/** 单字宽度：宽字符 1，半角 0.5（与 subtitles.py 的 `_char_w` 同口径）。 */
export const charWidth = (ch: string): number => (isWideChar(ch) ? 1 : 0.5);

/**
 * 四舍五入到整数 —— 但**半值取偶**（round half to even）。
 * 为什么要跟 Python 一样用「取偶」而不是 Math.round 的「半值进位」：
 * `scripts/subtitles.py` 的 `_vis_len()` 是 `int(round(total))`，Python 的 round 是半值取偶；
 * 一条字幕恰好 18.5 个字宽时脚本算 18、这里若算 19，两边的 maxChars 判据就会在临界句上分叉。
 * 宽度都是 0.5 的倍数，浮点求和在二进制里是精确的，不会出现 18.500000000000004 这类误差。
 */
const roundHalfToEven = (value: number): number => {
  const lower = Math.floor(value);
  const diff = value - lower;
  if (diff > 0.5) return lower + 1;
  if (diff < 0.5) return lower;
  return lower % 2 === 0 ? lower : lower + 1;
};

/** 「字数」：宽 1 / 半角 0.5，四舍五入（半值取偶，与 subtitles.py 的 `_vis_len` 同口径）。 */
export const visLen = (text: string): number => {
  let total = 0;
  for (const ch of text) total += charWidth(ch);
  return roundHalfToEven(total);
};

/**
 * 拉丁词 / URL / 数字：字母数字开头结尾，内部允许 URI 里常见的连接符。
 * 折行时整段不可拆 —— 否则 `xiaozhangben.app` 会被切成两行各半个域名；
 * 带 `scheme://` 的整段也算一个原子，否则 `https://example.com/x` 会从 `https://`
 * 后面被静默拆行。
 * 与 `scripts/subtitles.py` 的 `LATIN_TOKEN_RE` 逐字一致（含 scheme 前缀，R5-subs-03）。
 */
const LATIN_TOKEN_RE = /(?:[A-Za-z][A-Za-z0-9+.\-]*:\/\/)?[A-Za-z0-9]+(?:[._\-/:@#?=&%][A-Za-z0-9]+)*/y;

/** 把文本拆成折行原子：CJK / 标点 / 空格逐字，拉丁词与 URL 整段一个原子。 */
export const splitAtoms = (text: string): string[] => {
  const atoms: string[] = [];
  let i = 0;
  while (i < text.length) {
    LATIN_TOKEN_RE.lastIndex = i;
    const match = LATIN_TOKEN_RE.exec(text);
    if (match && match.index === i && match[0].length > 0) {
      atoms.push(match[0]);
      i += match[0].length;
    } else {
      // 必须按 code point 取字：BMP 之外的「𠮷」这类字是一个代理对，
      // 按 text[i] 取会被切成两个原子（R6：与 Python 逐字符迭代对齐）。
      const ch = String.fromCodePoint(text.codePointAt(i) as number);
      atoms.push(ch);
      i += ch.length;
    }
  }
  return atoms;
};

/** 原子的字数宽度。 */
const atomWidth = (atom: string): number => {
  let total = 0;
  for (const ch of atom) total += charWidth(ch);
  return total;
};

/** 前 upto 个原子一共占多少个字符。 */
const atomsChars = (atoms: readonly string[], upto: number): number =>
  atoms.slice(0, upto).reduce((sum, atom) => sum + atom.length, 0);

/** 字符下标 → 原子下标；charCut 不在原子边界上时返回 -1。 */
const charCutAtom = (atoms: readonly string[], charCut: number): number => {
  let acc = 0;
  for (let index = 0; index < atoms.length; index += 1) {
    if (acc === charCut) return index;
    acc += atoms[index].length;
  }
  return acc === charCut ? atoms.length : -1;
};

/** 这个切点合法吗：两边都不是标点（切点两侧不能是收尾/起始标点）。 */
export const validBreak = (tokens: readonly string[], cut: number): boolean => {
  if (cut <= 0 || cut >= tokens.length) return false;
  if (CLOSING_PUNCT.includes(tokens[cut]) || OPENING_PUNCT.includes(tokens[cut - 1])) return false;
  return true;
};

/**
 * 从 text 头部取一行：返回 `{ cut 切点字符数, overflow 超宽字数 }`。
 * 先贪心装到 width 拿到上界，再往前找最后一个合法切点；找不到就容许放行 1~2 个
 * 收尾标点；唯一允许硬切的情况是「单个拉丁词本身比整行还宽」。
 */
const wrapLine = (text: string, width: number): { cut: number; overflow: number } => {
  const atoms = splitAtoms(text);
  let consumed = 0;
  let fit = 0;
  while (fit < atoms.length) {
    const w = atomWidth(atoms[fit]);
    if (consumed + w > width) break;
    consumed += w;
    fit += 1;
  }

  if (fit <= 0) {
    // 第一个原子就比整行还宽：只能在这一个词内部硬切（不会丢字）。
    const chars = Array.from(atoms[0]);
    let hard = 0;
    let acc = 0;
    while (hard < chars.length && acc + charWidth(chars[hard]) <= width) {
      acc += charWidth(chars[hard]);
      hard += 1;
    }
    return { cut: Math.max(1, Math.min(hard, text.length - 1)), overflow: 0 };
  }

  // 全部原子都装得下时（平衡分支的 mid 可能给得比整行还宽），退一个保证有剩余。
  fit = Math.min(fit, atoms.length - 1);

  // 主选：上界以内的最后一个合法切点。
  let cut = fit;
  while (cut > 0 && !validBreak(atoms, cut)) cut -= 1;
  if (cut > 0) return { cut: atomsChars(atoms, cut), overflow: 0 };

  // 退让：上界之后最近的合法切点，只要多装的都是收尾标点且不超宽容度。
  let over = fit;
  let extra = 0;
  while (over < atoms.length - 1 && extra < FOLD_OVERFLOW_GRACE) {
    if (!CLOSING_PUNCT.includes(atoms[over])) break;
    extra += charWidth(atoms[over]);
    over += 1;
    if (validBreak(atoms, over)) return { cut: atomsChars(atoms, over), overflow: extra };
  }
  return { cut: atomsChars(atoms, fit), overflow: 0 };
};

/** 第二行以「单个字 + 收尾标点」开头 = 中文词被拆开的指纹（u 让「𠮷」按一个 code point 算）。
 *  空白类不用 JS 的 `\s`：它含 U+FEFF，而 Python 的 `\s` 不含（R7-TS-02）。 */
const LONELY_HEAD_RE = new RegExp(
  `^[^${PY_SPACE_CLASS}，。、；：！？,.;:!?…—][${PY_SPACE_CLASS}]*[，。、；：！？,.;:!?…—]`,
  "u",
);

export const lonelyHead = (tail: string): boolean => LONELY_HEAD_RE.test(tail);

/**
 * 在中心切点附近的原子边界里挑「两行最均衡」的合法切点。
 * 均衡优先（两行字数差最小），标点边界只作为同分时的加分项 —— 这样「小票」这类
 * 中文词不会为了凑标点被拆开。返回 0 表示 center 附近没有合法切点。
 */
const balanceCut = (rest: string, center: number, width: number): number => {
  // center 是**码点**下标（与 Python `_fold_text` 的 cut 同口径）；切片仍要用 UTF-16 下标，
  // 所以这里同时维护「码点边界」与「码元边界」两套坐标（R7-TS-01）。
  const atoms = splitAtoms(rest);
  const cpBounds: number[] = [0];
  const unitBounds: number[] = [0];
  let cpAcc = 0;
  let unitAcc = 0;
  for (const atom of atoms) {
    cpAcc += codePointLength(atom);
    unitAcc += atom.length;
    cpBounds.push(cpAcc);
    unitBounds.push(unitAcc);
  }
  const index = cpBounds.indexOf(center);
  if (index < 0) return 0;
  let bestScore: readonly [number, number] = [10 ** 9, 10 ** 9];
  let bestCut = 0;
  for (let offset = -BALANCE_LOOKBACK; offset <= BALANCE_LOOKBACK; offset += 1) {
    const atomCut = index + offset;
    if (atomCut <= 0 || atomCut >= atoms.length || !validBreak(atoms, atomCut)) continue;
    const cand = unitBounds[atomCut];
    const head = visLen(rest.slice(0, cand));
    const tail = visLen(rest.slice(cand));
    if (head > width || tail > width) continue;
    // 前一个字必须按码点取（BMP 外字符用 rest[cand-1] 会拿到代理对的一半）。
    const before = Array.from(rest.slice(0, cand)).pop();
    const afterPunctOrSpace = before !== undefined && (CLOSING_PUNCT.includes(before) || before === " ");
    const score: readonly [number, number] = [Math.abs(head - tail), afterPunctOrSpace ? 0 : 1];
    if (score[0] < bestScore[0] || (score[0] === bestScore[0] && score[1] < bestScore[1])) {
      bestScore = score;
      bestCut = cand;
    }
  }
  return bestCut;
};

/**
 * 归一化空白：先剥 C0/C1 控制字符，再把换行 / 制表 / 全角空格压成半角空格并修剪
 * （与脚本 `_clean_text` 逐条一致，R7-TS-02）。
 */
export const cleanText = (text: string): string =>
  String(text ?? "")
    .replace(CONTROL_CHARS_RE, "")
    .replace(PY_SPACE_RUN_RE, " ")
    .replace(PY_SPACE_TRIM_RE, "");

/** 码点数（Python `len(str)` 口径：BMP 外字符算 1 个，UTF-16 代理对算 2 个码元）。 */
const codePointLength = (text: string): number => Array.from(text).length;

/** 把 UTF-16 下标往左挪一个**码点**（BMP 外的字符是代理对，不能只减 1）。 */
const prevCodePointCut = (text: string, cut: number): number => {
  if (cut <= 0) return 0;
  const low = text.charCodeAt(cut - 1);
  return low >= 0xdc00 && low <= 0xdfff ? cut - 2 : cut - 1;
};

/**
 * Python `_int()` 口径的宽松整数：数字（浮点按截断）、纯整数字符串、布尔可转，
 * 其余（缺键 / NaN / 对象 / 小数串）一律回退 fallback（`subtitles.py::_int`，R7-TS-RO2）。
 */
const toIntLoose = (value: unknown, fallback: number): number => {
  if (typeof value === "number") return Number.isFinite(value) ? Math.trunc(value) : fallback;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "string") {
    const trimmed = value.trim();
    return /^[+-]?\d+$/.test(trimmed) ? Number.parseInt(trimmed, 10) : fallback;
  }
  return fallback;
};

/** `captions.maxChars` 兜底：非法（缺键 / 非数字 / NaN / ≤0）一律回退 13，与脚本 `_int(...,13)` 同口径。 */
export const normalizeMaxChars = (value: unknown): number => {
  const parsed = toIntLoose(value, 13);
  return parsed > 0 ? parsed : 13;
};

/** `captions.maxLines` 兜底：非法（缺键 / 非数字 / NaN / ≤0）一律回退 2，与脚本 `_int(...,2)` 同口径。 */
export const normalizeMaxLines = (value: unknown): number => {
  const parsed = toIntLoose(value, 2);
  return parsed > 0 ? parsed : 2;
};

/**
 * 按字数折行，**不丢字**：返回的行数可能超过 maxLines（超行由脚本侧拦下；
 * 组件侧由 `wrapCJK` 兜底截尾）。算法见文件头注释。
 */
export const foldText = (text: string, maxChars: number): string[] => {
  const cleaned = cleanText(text);
  if (!cleaned) return [];
  const width = Math.max(1, maxChars);
  const lines: string[] = [];
  let rest = cleaned;
  while (rest) {
    if (visLen(rest) <= width) {
      lines.push(pyStrip(rest));
      break;
    }
    const wrapped = wrapLine(rest, width);
    let cut = Math.max(1, Math.min(wrapped.cut, rest.length - 1));
    let tail = rest.slice(cut);
    // 平衡：首行偏长、剩下的字一行还装得下时，把切点挪到中间（两行差不多长更好看）。
    // 阈值必须用**码点**口径（R7-TS-01）：Python `_fold_text` 是 `cut > len(rest) / 2`，
    // 而 TS 的 cut / rest.length 是 UTF-16 码元数 —— 句子里有 BMP 外字符（𠮷 / emoji）时会差半个字。
    if (
      tail &&
      visLen(tail) <= width &&
      codePointLength(rest.slice(0, cut)) > codePointLength(rest) / 2
    ) {
      const atoms = splitAtoms(rest);
      const half = atoms.reduce((sum, atom) => sum + atomWidth(atom), 0) / 2;
      let acc = 0;
      let center = 0; // 码点下标（与 Python 的 cut 口径一致）
      for (const atom of atoms) {
        if (acc >= half) break;
        acc += atomWidth(atom);
        center += codePointLength(atom);
      }
      const midCut = balanceCut(rest, center, width);
      if (
        midCut > 0 &&
        midCut < rest.length &&
        visLen(rest.slice(0, midCut)) <= width &&
        visLen(rest.slice(midCut)) <= width
      ) {
        cut = midCut;
        tail = rest.slice(midCut);
      }
    }
    // 第二行以「单字 + 收尾标点」开头时，切点左移 1 字（中文词不被拆开）。
    // 左移必须按码点走：BMP 外字符是代理对，cut - 1 会切在半个字符上（R7-TS-01）。
    const prevCut = prevCodePointCut(rest, cut);
    if (lonelyHead(tail) && prevCut > 0 && visLen(rest.slice(prevCut)) <= width) {
      const atoms = splitAtoms(rest);
      const atomCut = charCutAtom(atoms, prevCut);
      if (atomCut > 0 && validBreak(atoms, atomCut)) {
        cut = prevCut;
        tail = rest.slice(cut);
      }
    }
    lines.push(pyStrip(rest.slice(0, cut)));
    rest = tail;
  }
  return lines;
};

/**
 * <Caption> 用的入口：与 `subtitles.py` 的 `_fold_text` 同一套折行；
 * 唯一区别是这里**保证不超过 maxLines 行**（组件里不许崩，也不许把字画到安全区外）。
 * 整句超过 maxChars × maxLines 个**字宽**时，按字宽预算截尾加省略号再折行 ——
 * 不能按 `Array.from(text).length`（原始字符数）截：`https://example.com/x` 是 21 个字符
 * 但只有 10.5 个字宽，按字符数截会把明明放得下的句子误截（R5-TPL-02 回归探针 case 5）。
 */
export const wrapCJK = (text: string, maxChars: number, maxLines: number, ellipsis = "…"): string[] => {
  // 非法 / 缺失的 captions.maxChars / maxLines 必须按脚本 `_int()` 口径回退默认值（R7-TS-RO2）：
  // 否则 `Math.max(1, NaN)` 还是 NaN，会一路变成「不折行 + 字号 NaN + 溢出安全区」且不报错。
  const perLine = normalizeMaxChars(maxChars);
  const linesCount = normalizeMaxLines(maxLines);
  const capacity = perLine * linesCount;
  const cleaned = cleanText(text);
  const lines = foldText(cleaned, perLine);
  if (lines.length <= linesCount) return lines;

  // 超行（正常输入到不了这里：subtitles.py 会直接 ERROR 让作者改稿）：
  // 按字宽预算截断，给避头尾的宽容度留 2 个字，再折一次。
  const budget = Math.max(1, capacity - FOLD_OVERFLOW_GRACE);
  let cutText = "";
  let usedWidth = 0;
  for (const ch of cleaned) {
    const w = charWidth(ch);
    if (usedWidth + w > budget) break;
    cutText += ch;
    usedWidth += w;
  }
  const truncated = `${pyTrimEnd(cutText)}${ellipsis}`;
  const wrapped = foldText(truncated, perLine);
  if (wrapped.length <= linesCount) return wrapped;
  // 极端兜底（maxLines=1 之类）：整句压成一行，宁可溢出也不多画一行
  return [truncated];
};
