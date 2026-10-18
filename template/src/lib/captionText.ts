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
 * 东亚洲宽字符（Unicode EAW 的 W/F 区段）判断。
 * 只覆盖字幕/文案会用到的区段；`…`(U+2026)、`—`(U+2014) 在 EAW 里是 A（ambiguous），
 * subtitles.py 的 `_vis_len` 也按 0.5 算，这里保持一致、不列进宽字符。
 */
export const isWideChar = (ch: string): boolean => {
  const cp = ch.codePointAt(0) ?? 0;
  return (
    (cp >= 0x1100 && cp <= 0x115f) || // 韩文字母
    (cp >= 0x2e80 && cp <= 0x303e) || // CJK 部首 / 符号 / 标点（含「。」「、」）
    (cp >= 0x3041 && cp <= 0x33ff) || // 假名 / CJK 兼容
    (cp >= 0x3400 && cp <= 0x4dbf) || // CJK 扩展 A
    (cp >= 0x4e00 && cp <= 0x9fff) || // CJK 基本区
    (cp >= 0xa000 && cp <= 0xa4cf) || // 彝文
    (cp >= 0xac00 && cp <= 0xd7a3) || // 韩文音节
    (cp >= 0xf900 && cp <= 0xfaff) || // CJK 兼容汉字
    (cp >= 0xfe10 && cp <= 0xfe19) || // 竖排标点
    (cp >= 0xfe30 && cp <= 0xfe6f) || // CJK 兼容形式
    (cp >= 0xff00 && cp <= 0xff60) || // 全角 ASCII（「，」「！」「？」等）
    (cp >= 0xffe0 && cp <= 0xffe6) // 全角符号
  );
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
      atoms.push(text[i]);
      i += 1;
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

/** 第二行以「单个字 + 收尾标点」开头 = 中文词被拆开的指纹。 */
const LONELY_HEAD_RE = /^[^\s，。、；：！？,.;:!?…—]\s*[，。、；：！？,.;:!?…—]/;

export const lonelyHead = (tail: string): boolean => LONELY_HEAD_RE.test(tail);

/**
 * 在中心切点附近的原子边界里挑「两行最均衡」的合法切点。
 * 均衡优先（两行字数差最小），标点边界只作为同分时的加分项 —— 这样「小票」这类
 * 中文词不会为了凑标点被拆开。返回 0 表示 center 附近没有合法切点。
 */
const balanceCut = (rest: string, center: number, width: number): number => {
  const atoms = splitAtoms(rest);
  const boundaries: number[] = [0];
  let acc = 0;
  for (const atom of atoms) {
    acc += atom.length;
    boundaries.push(acc);
  }
  const index = charCutAtom(atoms, center);
  if (index < 0) return 0;
  let bestScore: readonly [number, number] = [10 ** 9, 10 ** 9];
  let bestCut = 0;
  for (let offset = -BALANCE_LOOKBACK; offset <= BALANCE_LOOKBACK; offset += 1) {
    const atomCut = index + offset;
    if (atomCut <= 0 || atomCut >= atoms.length || !validBreak(atoms, atomCut)) continue;
    const cand = boundaries[atomCut];
    const head = visLen(rest.slice(0, cand));
    const tail = visLen(rest.slice(cand));
    if (head > width || tail > width) continue;
    const before = rest[cand - 1];
    const afterPunctOrSpace = before !== undefined && (CLOSING_PUNCT.includes(before) || before === " ");
    const score: readonly [number, number] = [Math.abs(head - tail), afterPunctOrSpace ? 0 : 1];
    if (score[0] < bestScore[0] || (score[0] === bestScore[0] && score[1] < bestScore[1])) {
      bestScore = score;
      bestCut = cand;
    }
  }
  return bestCut;
};

/** 归一化空白：换行 / 制表 / 全角空格都换成半角空格并压缩（与脚本 `_clean_text` 一致）。 */
export const cleanText = (text: string): string => {
  const raw = String(text ?? "").replace(/[\t\u3000\u00a0]/g, " ");
  return raw.replace(/\s+/g, " ").trim();
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
      lines.push(rest.trim());
      break;
    }
    const wrapped = wrapLine(rest, width);
    let cut = Math.max(1, Math.min(wrapped.cut, rest.length - 1));
    let tail = rest.slice(cut);
    // 平衡：首行偏长、剩下的字一行还装得下时，把切点挪到中间（两行差不多长更好看）。
    if (tail && visLen(tail) <= width && cut > rest.length / 2) {
      const atoms = splitAtoms(rest);
      const half = atoms.reduce((sum, atom) => sum + atomWidth(atom), 0) / 2;
      let acc = 0;
      let center = 0;
      for (const atom of atoms) {
        if (acc >= half) break;
        acc += atomWidth(atom);
        center += atom.length;
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
    if (lonelyHead(tail) && cut > 1 && visLen(rest.slice(cut - 1)) <= width) {
      const atoms = splitAtoms(rest);
      const atomCut = charCutAtom(atoms, cut - 1);
      if (atomCut > 0 && validBreak(atoms, atomCut)) {
        cut -= 1;
        tail = rest.slice(cut);
      }
    }
    lines.push(rest.slice(0, cut).trim());
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
  const perLine = Math.max(1, maxChars);
  const linesCount = Math.max(1, maxLines);
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
  const truncated = `${cutText.trimEnd()}${ellipsis}`;
  const wrapped = foldText(truncated, perLine);
  if (wrapped.length <= linesCount) return wrapped;
  // 极端兜底（maxLines=1 之类）：整句压成一行，宁可溢出也不多画一行
  return [truncated];
};
