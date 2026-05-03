const MAX_WORD_LEN = 8;
const OOV_PENALTY = Math.log(50_001);
const LENGTH_BONUS = 0.5;
const NO_FREQ_KNOWN = 200;
const NO_FREQ_UNKNOWN = 8000;
const HSK123 = "的 我 你 是 了 不 在 他 我们 好 有 这 会 吗 什么 说 她 想 一 很 人 那 来 都 个 能 去 和 做 上 没有 看 怎么 现在 点 呢 太 里 听 谁 多 时候 下 谢谢 先生 喜欢 大 东西 小 叫 爱 年 请 回 工作 钱 吃 开 家 哪 朋友 妈妈 今天 几 爸爸 些 怎么样 对不起 住 三 高兴 买 医生 哪儿 字 名字 认识 坐 喝 写 月 号 狗 岁 看见 打电话 喂 儿子 漂亮 分钟 再见 本 明天 少 多少 块 女儿 小姐 衣服 水 学校 电影 书 四 五 医院 没关系 飞机 二 电视 读 后面 昨天 睡觉 六 老师 星期 十 猫 电脑 热 学生 下午 学习 冷 不客气 前面 八 中国 七 菜 桌子 出租车 天气 茶 九 商店 椅子 同学 一点儿 苹果 饭店 中午 上午 水果 杯子 下雨 米饭 北京 汉语 就 要 知道 吧 到 对 也 还 让 给 过 得 真 着 可以 别 走 告诉 因为 再 快 但是 已经 为什么 觉得 它 从 找 最 可能 次 出 孩子 所以 两 错 等 题 问 问题 一起 开始 时间 事情 一下 非常 希望 准备 比 件 意思 第一 进 大家 新 您 穿 送 玩 长 小时 完 每 公司 帮助 晚上 说话 门 女 忙 卖 高 房间 路 懂 正在 笑 远 妻子 丈夫 离 往 男 眼睛 快乐 虽然 早上 药 身体 黑 咖啡 日 休息 外 生日 哥哥 票 手机 洗 跳舞 弟弟 妹妹 红 慢 近 白 姐姐 介绍 鱼 累 课 上班 旁边 运动 去年 报纸 颜色 机场 唱歌 千 好吃 考试 左边 姓 百 雪 贵 生病 游泳 牛奶 右边 便宜 公共汽车 起床 打篮球 鸡蛋 踢足球 零 手表 旅游 服务员 宾馆 教室 跑步 阴 面条 铅笔 火车站 西瓜 羊肉 晴 啊 还 把 过 如果 只 被 跟 自己 用 像 为 需要 应该 起来 才 又 拿 更 带 然后 一样 当然 相信 认为 明白 一直 地 地方 离开 一定 还是 发 发现 而且 必须 放 为了 向 老 位 先 种 最后 其他 记得 或者 过去 担心 条 以前 长 世界 重要 别人 机会 张 接 比赛 关 关系 马 马上 决定 关于 难 了解 站 结束 清楚 愿意 花 照片 欢迎 总是 嘴 参加 办法 选择 坏 打算 试 特别 注意 其实 小心 久 只有 讲 故事 换 结婚 段 努力 害怕 刚才 节目 辆 万 解决 办公室 奇怪 同意 游戏 帮忙 国家 最近 声音 可爱 分 完成 半 要求 除了 容易 教 脸 简单 检查 音乐 越 照顾 聪明 甜 突然 终于 船 口 回答 礼物 头发 关心 脚 生气 哭 画 年轻 包 腿 忘记 搬 楼 遇到 新闻 比较 双 见面 经常 城市 一会儿 附近 借 影响 认真 米 差 银行 安静 多么 饿 根据 几乎 后来 动物 西 一边 舒服 一般 叔叔 疼 迟到 历史 啤酒 短 经过 周末 班 习惯 公园 干净 鸟 健康 树 蛋糕 元 客人 会议 奶奶 裤子 邻居 经理 层 灯 练习 爷爷 蓝 难过 中间 帽子 司机 旧 满意 骑 太阳 极 主要 同事 鼻子 角 变化 东 年级 环境 胖 地图 面包 电子邮件 耳朵 裙子 新鲜 放心 聊天 南 热情 信用卡 电梯 方便 洗手间 洗澡 饮料 校长 水平 作业 衬衫 成绩 碗 阿姨 图书馆 文化 绿 打扫 草 冰箱 数学 自行车 着急 瘦 提高 起飞 矮 地铁 体育 刻 护照 节日 盘子 一共 瓶子 街道 锻炼 感冒 爱好 超市 月亮 饱 有名 香蕉 笔记本 夏 菜单 北方 上网 季节 渴 发烧 不但 空调 照相机 春 伞 公斤 个子 熊猫 刷牙 复习 冬 请假 中文 秋 句子 行李箱 筷子 皮鞋 爬山 黑板 感兴趣 词典 留学 刮风 黄河".split(" ");
const NUMBERS = new Set("零一二三四五六七八九十百千万亿两");
// Stored after init so the Show button can re-annotate without re-fetching
let _dict = null;
let _known = null;

const PUNCT_RE =
  /^[\s\p{P}\p{Z}\p{C}！？。，、；：""''「」『』【】〔〕…—～·×÷]+$/u;
const NUMBER_RE = /^[0-9０-９]+$/u;

function isPunct(s) {
  return s.length > 0 && (PUNCT_RE.test(s) || NUMBER_RE.test(s));
}

function isCompositionallyKnown(word, known) {
  // Pure Chinese numbers: 一百, 三十四, 两千, 零...
  if (word.length > 0 && [...word].every(ch => NUMBERS.has(ch))) {
    return true;
  }

  // 们 pluralization: 他→他们, 我→我们, etc.
  if (word.length >= 2 && word.endsWith("们")) {
    return known.has(word.slice(0, -1));
  }

  // 第X ordinals: 第一, 第二, 第三...
  if (word.length >= 2 && word.startsWith("第")) {
    return [...word.slice(1)].every(ch => NUMBERS.has(ch));
  }

  // 不 + known word negation (skip known idioms)
  const BU_IDIOMS = new Set(["不得了", "不要紧", "不得不", "不由得"]);
  if (word.length >= 2 && word.startsWith("不") && !BU_IDIOMS.has(word)) {
    return known.has(word.slice(1));
  }

  return false;
}

function buildKnownSet() {
  const deckWords = new Set(window.MOGAO_CONFIG.deckWords);
  return new Set([...deckWords, ...HSK123]);
}

// DP Segmentation
function segment(sentence, dict, known) {
  const n = sentence.length;
  if (n === 0) return [];

  const dp = new Array(n + 1).fill(null);
  dp[0] = { oov: 0, logRank: 0, segs: 0, words: [] };

  for (let i = 1; i <= n; i++) {
    for (let j = Math.max(0, i - MAX_WORD_LEN); j < i; j++) {
      if (!dp[j]) continue;
      const word = sentence.slice(j, i);

      let oovAdd, rankAdd;
      if (isPunct(word)) {
        oovAdd = 0;
        rankAdd = 0;
      } else if (dict[word]) {
        oovAdd = 0;
		const fallback = known.has(word) ? NO_FREQ_KNOWN : NO_FREQ_UNKNOWN;
		const rank = dict[word].frequency ?? fallback;
        rankAdd = Math.log(rank + 1) - LENGTH_BONUS * (word.length - 1);
      } else {
        oovAdd = word.length;
        rankAdd = OOV_PENALTY * word.length;
      }

      const c = {
        oov: dp[j].oov + oovAdd,
        logRank: dp[j].logRank + rankAdd,
        segs: dp[j].segs + 1,
        words: [...dp[j].words, word],
      };

      if (
        !dp[i] ||
        c.oov < dp[i].oov ||
        (c.oov === dp[i].oov && c.logRank < dp[i].logRank) ||
        (c.oov === dp[i].oov && c.logRank === dp[i].logRank && c.segs < dp[i].segs)
      ) {
        dp[i] = c;
      }
    }
  }

  return dp[n]?.words ?? [...sentence];
}

// Classification

const SENTENCE_END_RE = /(?<=[。！？…]+)/u;

function classifyWord(word, dict, known) {
  if (isPunct(word)) return "punct";
  if (!dict[word]) return "oov";
  if (known.has(word) || isCompositionallyKnown(word, known)) return "known";
  return "unknown";
}

/**
 * Analyze a single sentence. If there is exactly one unknown word and no OOV
 * segments, promote that word to "i1" — a true i+1 target.
 */
function analyzeSentence(sentence, dict, known) {
  const words = segment(sentence, dict, known).map((w) => ({
    word: w,
    status: classifyWord(w, dict, known),
  }));

  const unknowns = words.filter((w) => w.status === "unknown");
  const hasOov = words.some((w) => w.status === "oov");

  if (unknowns.length === 1 && !hasOov) {
    unknowns[0].status = "i1";
  }

  return words;
}

function splitSentences(text) {
  return text.split(SENTENCE_END_RE).filter((s) => s.length > 0);
}

const STATUS_CLASS = {
  known: "seg-known",
  unknown: "seg-unknown",
  i1: "seg-i1",
  oov: "seg-oov",
};


function annotateNode(root, dict, known) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (node.parentElement?.className?.includes("seg-"))
        return NodeFilter.FILTER_REJECT;
      const tag = node.parentElement?.tagName;
      if (tag === "SCRIPT" || tag === "STYLE") return NodeFilter.FILTER_REJECT;
      if (/[\u4e00-\u9fff]/.test(node.textContent)) return NodeFilter.FILTER_ACCEPT;
      return NodeFilter.FILTER_SKIP;
    },
  });

  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);

  for (const textNode of nodes) {
    const sentences = splitSentences(textNode.textContent);
    if (!sentences.length) continue;

    const frag = document.createDocumentFragment();
    for (const sentence of sentences) {
      const analyzed = analyzeSentence(sentence, dict, known);
      for (const { word, status } of analyzed) {
        if (status === "punct") {
          frag.appendChild(document.createTextNode(word));
        } else {
          const span = document.createElement("span");
          span.className = STATUS_CLASS[status];
          span.textContent = word;
          frag.appendChild(span);
        }
      }
    }
    textNode.parentNode.replaceChild(frag, textNode);
  }
}

function stripAnnotations(root) {
  for (const span of root.querySelectorAll(".seg-known,.seg-unknown,.seg-i1,.seg-oov")) {
    const p = span.parentNode;
    while (span.firstChild) p.insertBefore(span.firstChild, span);
    p.removeChild(span);
  }
  root.normalize();
}


function buildUI(bookContent) {
  const toggleBtn = document.getElementById("seg-toggle");

  if (toggleBtn && !toggleBtn.dataset.segInitialized) {
    const MODES = ["i1", "all", "off"];
    
	toggleBtn.addEventListener("click", () => {
		let currentMode = bookContent.getAttribute("data-seg-mode") || "i1";
		let nextMode = MODES[(MODES.indexOf(currentMode) + 1) % MODES.length];

		bookContent.setAttribute("data-seg-mode", nextMode);
        refreshStatsPopup(bookContent);
	});
	toggleBtn.dataset.segInitialized = "true";
  }
}


// Public entry point — call from dictionary.js after localDict is set
async function annotateBookContent(dict, known) {
  const bookContent = document.querySelector(".book-content");
  if (!bookContent) return;

  _dict = dict;
  _known = known;

  // Initialize the mode to 'i1' by default if not set
  if (!bookContent.hasAttribute('data-seg-mode')) {
    bookContent.setAttribute('data-seg-mode', 'i1');
  }

  // Parse text only once; TreeWalker ignores elements already wrapped in '.seg-'
  annotateNode(bookContent, dict, known);
  buildUI(bookContent);
}

window.reannotateWithNewDict = function (newDict) {
  if (!newDict || !_known) return;
  const bookContent = document.querySelector(".book-content");
  if (!bookContent) return;
 
  _dict = newDict;
  stripAnnotations(bookContent);
  annotateNode(bookContent, newDict, _known);
};
