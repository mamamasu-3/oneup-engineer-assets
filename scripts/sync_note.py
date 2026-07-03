#!/usr/bin/env python3
"""note.com の記事を監視し、新規記事の画像をダウンロードして Discord に通知する。

- 標準ライブラリのみで動作（依存パッケージ不要）
- 処理済み記事は images/.processed.json で管理し、差分（新規記事）だけを処理する
- 新規記事があれば画像を images/yyyy-MM-dd/ に保存し、Discord Webhook に通知する

環境変数:
  DISCORD_WEBHOOK_URL  Discord の Webhook URL（未設定なら通知はスキップ）
  NOTE_CREATOR         note のクリエイター名（既定: oneup_engineer）
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

CREATOR = os.environ.get("NOTE_CREATOR", "oneup_engineer")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(ROOT, "images")
MANIFEST = os.path.join(IMAGES_DIR, ".processed.json")

UA = {"User-Agent": "Mozilla/5.0 (compatible; note-sync-bot/1.0)"}
# 本文中の st-note 画像URL（クエリ除く拡張子で判定）
IMG_RE = re.compile(
    r"https://assets\.st-note\.com/[^\s\"'\\)]+?\.(?:jpe?g|png|gif|webp)",
    re.IGNORECASE,
)


def http_json(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))
    return None


def load_manifest():
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(m):
    os.makedirs(IMAGES_DIR, exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        f.write(json.dumps(m, ensure_ascii=False, indent=2) + "\n")


def list_articles():
    """全ページを走査して記事の概要リストを返す。"""
    out = []
    page = 1
    while True:
        d = http_json(
            f"https://note.com/api/v2/creators/{CREATOR}/contents?kind=note&page={page}"
        )["data"]
        for c in d["contents"]:
            out.append({"key": c["key"], "title": c["name"], "date": c["publishAt"][:10]})
        if d.get("isLastPage"):
            break
        page += 1
        if page > 50:  # 無限ループ保険
            break
    return out


def article_detail(key):
    d = http_json(f"https://note.com/api/v3/notes/{key}")["data"]
    body = d.get("body") or ""
    # 順序を保ちつつ重複排除
    seen = set()
    body_imgs = []
    for u in IMG_RE.findall(body):
        if u not in seen:
            seen.add(u)
            body_imgs.append(u)
    return {
        "eyecatch": d.get("eyecatch") or "",
        "note_url": f"https://note.com/{CREATOR}/n/{key}",
        "body_imgs": body_imgs,
    }


def ext_of(url):
    base = url.split("?", 1)[0]
    ext = base.rsplit(".", 1)[-1].lower()
    return ext if ext in ("jpg", "jpeg", "png", "gif", "webp") else "jpg"


def download(url, path):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with open(path, "wb") as f:
        f.write(data)


def process_article(key, date):
    detail = article_detail(key)
    date_dir = os.path.join(IMAGES_DIR, date)
    os.makedirs(date_dir, exist_ok=True)
    count = 0
    if detail["eyecatch"]:
        p = os.path.join(date_dir, f"thumbnail_{key}.{ext_of(detail['eyecatch'])}")
        download(detail["eyecatch"], p)
        count += 1
    for i, u in enumerate(detail["body_imgs"], 1):
        p = os.path.join(date_dir, f"{key}_body{i}.{ext_of(u)}")
        download(u, p)
        count += 1
    return count, detail


def notify_discord(article, image_count, detail):
    if not WEBHOOK:
        print("  DISCORD_WEBHOOK_URL 未設定のため通知スキップ")
        return
    embed = {
        "title": article["title"][:250],
        "url": detail["note_url"],
        "description": f"🆕 新しい記事が公開されました\n📅 {article['date']} ／ 🖼 画像 {image_count} 枚をダウンロードしました",
        "color": 0x2EA043,
    }
    if detail["eyecatch"]:
        embed["image"] = {"url": detail["eyecatch"]}
    payload = {
        "username": "note 監視Bot",
        "content": "note に新規記事が追加されました 🎉",
        "embeds": [embed],
    }
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  Discord通知: {r.status}")
    except urllib.error.HTTPError as e:
        print(f"  Discord通知失敗: {e.code} {e.read()[:200]!r}")


def main():
    manifest = load_manifest()
    articles = list_articles()
    new = [a for a in articles if a["key"] not in manifest]

    if not new:
        print(f"新規記事なし（監視対象 {len(articles)} 件）")
        # GitHub Actions 用の出力
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a") as f:
                f.write("changed=false\n")
        return

    print(f"新規記事 {len(new)} 件を検出")
    for a in new:
        print(f"- {a['date']} {a['key']} {a['title'][:40]}")
        count, detail = process_article(a["key"], a["date"])
        print(f"  画像 {count} 枚を保存")
        notify_discord(a, count, detail)
        manifest[a["key"]] = {"date": a["date"], "title": a["title"]}

    save_manifest(manifest)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write("changed=true\n")
            f.write(f"count={len(new)}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
