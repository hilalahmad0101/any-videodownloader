"""Facebook Reels titles + hashtags for a video.

Two engines:
  * Claude (best): used automatically when the `anthropic` package is installed and an
    ANTHROPIC_API_KEY is set (put it in a .env file next to run.sh). Writes fresh, hook-first
    titles in English (translating Chinese titles) and picks hashtags for the exact video.
  * Built-in (always works, offline): detects the topic from the title / tags / your search
    words and combines proven high-reach Facebook hashtags with the video's own tags.

Endpoints
    GET  /api/captions/status   -> which engine is active
    POST /api/captions          -> {titles: [3], caption, hashtags: [~15], engine}
"""
import os
import random
import re
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/captions")
CLAUDE_MODEL = "claude-opus-5-5"


class VideoIn(BaseModel):
    title: str
    description: Optional[str] = None
    tags: List[str] = []
    query: Optional[str] = None  # what you searched for (your niche)
    uploader: Optional[str] = None
    site: Optional[str] = None
    views: Optional[int] = None
    likes: Optional[int] = None


class CaptionOut(BaseModel):
    titles: List[str]
    caption: str
    hashtags: List[str]


# ---------------------------------------------------------------- built-in engine
BASE_TAGS = ["#reels", "#reelsfb", "#facebookreels", "#viralreels", "#fbreels", "#viral", "#trending", "#foryou"]

TOPICS = {
    # topic: (trigger words in en/zh, power hashtags, subject phrase)
    "funny": (
        ["funny", "comedy", "laugh", "lol", "hilarious", "joke", "meme", "搞笑", "爆笑", "幽默", "沙雕", "段子", "笑"],
        ["#funny", "#funnyvideos", "#comedy", "#funnyreels", "#trynottolaugh", "#hilarious", "#lol", "#memes"],
        "funny moment",
    ),
    "chinese": (
        ["china", "chinese", "douyin", "中国", "国内", "抖音", "bilibili", "beijing", "shanghai"],
        ["#china", "#chinese", "#chinesefunny", "#douyin", "#chinesecomedy", "#asia"],
        "Chinese",
    ),
    "prank": (["prank", "恶搞", "整蛊"], ["#prank", "#pranks", "#prankvideo", "#pranksters"], "prank"),
    "fail": (["fail", "fails", "bad luck", "失败", "翻车"], ["#fails", "#epicfail", "#failvideos", "#badluck"], "fail"),
    "food": (
        ["food", "cook", "cooking", "recipe", "street food", "eat", "美食", "做饭", "吃"],
        ["#food", "#streetfood", "#cooking", "#foodie", "#chinesefood", "#foodreels"],
        "food",
    ),
    "animals": (
        ["cat", "dog", "pet", "animal", "puppy", "kitten", "猫", "狗", "宠物", "动物"],
        ["#animals", "#funnyanimals", "#pets", "#cats", "#dogs", "#cuteanimals"],
        "animal",
    ),
    "kids": (["baby", "kid", "kids", "child", "宝宝", "孩子", "小孩", "萌娃"], ["#baby", "#cutebaby", "#kids", "#familyfun"], "kid"),
    "dance": (["dance", "dancing", "舞", "跳舞"], ["#dance", "#dancevideo", "#dancechallenge"], "dance"),
    "satisfying": (["satisfying", "解压", "asmr"], ["#satisfying", "#oddlysatisfying", "#asmr"], "satisfying clip"),
}

TITLE_TEMPLATES = [
    "Wait for the end 😂 {subject}",
    "I can't stop laughing 🤣 {subject}",
    "Nobody expected this… {subject} 😳",
    "This is why I love {topic} videos 😂",
    "Watch till the end, trust me 🤣",
    "{subject} — you won't believe the ending 😱",
    "Only in China 😂 {subject}",
    "Tag someone who needs to see this 😂",
    "This made my whole day 🤣 {subject}",
]

CTA = ["Follow for more 😂", "Share this with a friend who needs a laugh 🤣", "Comment 😂 if this got you!"]


def _hit(word: str, text: str) -> bool:
    if word.isascii():  # whole words only: "eat" must not match "great"
        return re.search(r"\b" + re.escape(word) + r"s?\b", text) is not None
    return word in text


def _detect(text: str) -> List[str]:
    t = text.lower()
    return [name for name, (words, _, _) in TOPICS.items() if any(_hit(w, t) for w in words)]


FILLER = re.compile(
    r"\b(19|20)\d\d\b|\bpart\s*\d+\b|\bep(isode)?\s*\d+\b|\b(whatsapp|vines?|tiktok|douyin|shorts?|compilation|"
    r"new|best of|official|full|hd|4k|1080p|video(s)?)\b",
    re.I,
)
WEAK_END = {"of", "and", "the", "a", "an", "to", "in", "on", "with", "for", "super", "best", "my", "your", "this", "that", "&"}


def _clean_subject(title: str) -> Optional[str]:
    """Short, readable subject from the original title (English-ish titles only)."""
    t = re.sub(r"#\S+", "", title)
    t = re.sub(r"[\[\(【（].*?[\]\)】）]", "", t)
    t = re.split(r"\s[|｜/-]\s", t)[0]
    t = re.split(r"[●•|｜]", t)[0] if len(re.split(r"[●•|｜]", t)[0].split()) >= 2 else t
    t = re.sub(r"[^\w\s'!?,&.-]", " ", t)  # drop emojis / symbols
    t = FILLER.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" -,.&")
    if not t or sum(c.isascii() for c in t) < len(t) * 0.8:
        return None  # mostly Chinese -> use the topic phrase instead
    words = t.split()
    while words and words[-1].lower() in WEAK_END:
        words.pop()
    if not 2 <= len(words) <= 6:  # too long would get cut mid-phrase: use the topic phrase instead
        return None
    return " ".join(w if w.isupper() and len(w) <= 4 else w.capitalize() for w in words)


def _tagify(tag: str) -> Optional[str]:
    tag = re.sub(r"[^\w]", "", tag.replace("#", "")).lower()
    if not tag or len(tag) > 24 or not tag.isascii() or tag.isdigit():
        return None
    return "#" + tag


def builtin(v: VideoIn) -> CaptionOut:
    text = " ".join([v.title, v.query or "", v.description or "", " ".join(v.tags)])
    topics = _detect(text) or ["funny"]
    if "funny" not in topics and any(w in text.lower() for w in ("搞笑", "funny")):
        topics.insert(0, "funny")

    # hashtags: topic power tags first, then the video's own tags, then broad reach tags
    # mix: ~8 topic tags, up to 3 of the video's own tags, 4-5 broad Facebook reach tags = 15
    topic_tags: List[str] = []
    for i, name in enumerate(topics[:3]):
        topic_tags += TOPICS[name][1][: 4 if i == 0 else 3]
    own = [t for t in (_tagify(x) for x in v.tags if not FILLER.search(x.replace("funny videos", "")))
           if t and t not in topic_tags][:3]
    hashtags = list(dict.fromkeys(topic_tags[:8] + own + BASE_TAGS))[:15]

    # titles
    topic_word = "Chinese funny" if {"funny", "chinese"} <= set(topics) else TOPICS[topics[0]][2]
    subject = _clean_subject(v.title) or f"{topic_word} {'moment' if 'moment' not in topic_word else ''}".strip().capitalize()
    templates = TITLE_TEMPLATES[:]
    if "chinese" not in topics:
        templates = [t for t in templates if "China" not in t]
    rnd = random.Random(v.title)  # same video -> same suggestions
    rnd.shuffle(templates)
    titles = [re.sub(r"\s+", " ", t.format(subject=subject, topic=topic_word.lower())).strip() for t in templates[:3]]

    caption = f"{titles[0]}\n\n{rnd.choice(CTA)}\n\n" + " ".join(hashtags)
    return CaptionOut(titles=titles, caption=caption, hashtags=hashtags)


# ---------------------------------------------------------------- Claude engine
def _claude_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic()


PROMPT = """You write Facebook Reels titles and hashtags for a short video someone is about to post.

Video details:
- Original title: {title}
- Creator: {uploader} ({site})
- Views / likes on the original: {views} / {likes}
- Original tags: {tags}
- Description: {description}
- Niche the poster searched for: {query}

Write:
1. titles: 3 different Facebook Reel titles, each under 70 characters, in English (translate if the
   original is Chinese). Lead with a hook (curiosity, surprise or humor) and use at most 2 emojis.
   Describe what is actually in the video; do not invent events or claim the poster made it.
2. caption: the best title, a blank line, one short call to action (follow / share / comment), a blank line,
   then the hashtags on one line.
3. hashtags: 15 hashtags, each starting with #, lowercase, no spaces. Mix about 5 broad high-reach Facebook
   tags (like #reels #viral #fbreels), about 7 tags for this video's topic, and about 3 niche tags. Only tags
   that are relevant to this video."""


def claude(v: VideoIn) -> Optional[CaptionOut]:
    client = _claude_client()
    if client is None:
        return None
    response = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        messages=[{
            "role": "user",
            "content": PROMPT.format(
                title=v.title, uploader=v.uploader or "unknown", site=v.site or "unknown",
                views=v.views if v.views is not None else "unknown", likes=v.likes if v.likes is not None else "unknown",
                tags=", ".join(v.tags[:30]) or "none", description=(v.description or "none")[:800],
                query=v.query or "none",
            ),
        }],
        output_format=CaptionOut,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return None
    out = response.parsed_output
    out.hashtags = list(dict.fromkeys("#" + h.lstrip("#").replace(" ", "").lower() for h in out.hashtags if h.strip()))[:15]
    return out


# ---------------------------------------------------------------- routes
@router.get("/status")
def status():
    return {"engine": "claude" if _claude_client() else "built-in"}


@router.post("")
def make_captions(v: VideoIn):
    if not v.title.strip():
        raise HTTPException(400, "Video title is missing.")
    try:
        out = claude(v)
        if out:
            return {**out.model_dump(), "engine": "claude"}
    except Exception:  # noqa: BLE001  (no credit, network, rate limit...: fall back, never fail)
        pass
    return {**builtin(v).model_dump(), "engine": "built-in"}
