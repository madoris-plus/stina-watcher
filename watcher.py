#!/usr/bin/env python3
"""stina「innocent bordeaux」柄の新着をメルカリ・ラクマで監視してメールで通知する。

判定:
  強一致  タイトルに柄名 (innocent, ボルドー など) を含む
  型一致  type J juliet で、配色もある程度近い
  候補    stina のレオタードで、サムネイルの配色 (ボルドー地 + 青い花) が近い
既読の商品IDは seen.json に保存し、同じ商品は二度通知しない。

環境変数:
  SMTP_USER    送信に使う Gmail アドレス（未設定なら標準出力に出すだけ）
  SMTP_PASS    その Gmail のアプリパスワード
  MAIL_TO      送信先（省略時は SMTP_USER 宛て）
  SMTP_HOST / SMTP_PORT  既定 smtp.gmail.com / 465
  SEED=1       初回用。今ある出品を既読にするだけで通知しない
  DRY_RUN=1    通知せず、判定結果を表示する
"""
import base64
import io
import json
import os
import re
import smtplib
import sys
import time
import uuid
from email.message import EmailMessage
from html import escape, unescape
from pathlib import Path

import numpy as np
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from PIL import Image

ROOT = Path(__file__).resolve().parent
SEEN_PATH = ROOT / "seen.json"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
S = requests.Session()
S.headers["User-Agent"] = UA
S.headers["Accept-Language"] = "ja-JP,ja;q=0.9"


# ---------------- メルカリ ----------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


_KEY = ec.generate_private_key(ec.SECP256R1())
_DEVICE = str(uuid.uuid4())


def _dpop(url: str, method: str) -> str:
    nums = _KEY.public_key().public_numbers()
    header = {"typ": "dpop+jwt", "alg": "ES256",
              "jwk": {"crv": "P-256", "kty": "EC",
                      "x": _b64(nums.x.to_bytes(32, "big")),
                      "y": _b64(nums.y.to_bytes(32, "big"))}}
    payload = {"iat": int(time.time()), "jti": str(uuid.uuid4()),
               "htu": url, "htm": method, "uuid": _DEVICE}
    signing = (_b64(json.dumps(header, separators=(",", ":")).encode()) + "." +
               _b64(json.dumps(payload, separators=(",", ":")).encode()))
    r, s = decode_dss_signature(_KEY.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    return signing + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def search_mercari(keyword: str):
    url = "https://api.mercari.jp/v2/entities:search"
    body = {
        "userId": "", "pageSize": 120, "pageToken": "",
        "searchSessionId": uuid.uuid4().hex,
        "indexRouting": "INDEX_ROUTING_UNSPECIFIED", "thumbnailTypes": [],
        "searchCondition": {
            "keyword": keyword, "excludeKeyword": "",
            "sort": "SORT_CREATED_TIME", "order": "ORDER_DESC",
            "status": ["STATUS_ON_SALE"],
            "sizeId": [], "categoryId": [], "brandId": [], "sellerId": [],
            "priceMin": 0, "priceMax": 0, "itemConditionId": [],
            "shippingPayerId": [], "shippingFromArea": [], "shippingMethod": [],
            "colorId": [], "hasCoupon": False, "attributes": [], "itemTypes": [],
            "skuIds": [],
        },
        "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"],
        "serviceFrom": "suruga", "withItemBrand": True, "withItemSize": False,
        "withItemPromotions": True, "withItemSizes": True, "withShopname": False,
    }
    res = S.post(url, json=body, timeout=30, headers={
        "DPoP": _dpop(url, "POST"), "X-Platform": "web",
        "Content-Type": "application/json; charset=utf-8"})
    res.raise_for_status()
    out = []
    for it in res.json().get("items", []):
        shop = it.get("itemType") == "ITEM_TYPE_BEYOND"
        out.append({
            "site": "メルカリ",
            "id": "m:" + it["id"],
            "title": it.get("name", ""),
            "price": int(it.get("price") or 0),
            "thumb": (it.get("thumbnails") or [None])[0],
            "url": (f"https://jp.mercari.com/shops/product/{it['id']}" if shop
                    else f"https://jp.mercari.com/item/{it['id']}"),
        })
    return out


# ---------------- ラクマ ----------------
_RAKUMA_LINK = re.compile(r'href="(https://item\.fril\.jp/([0-9a-f]+))"')


def search_rakuma(keyword: str):
    res = S.get("https://fril.jp/s", timeout=30, params={
        "query": keyword, "sort": "created_at", "order": "desc", "transaction": "selling"})
    res.raise_for_status()
    out = []
    # 商品ごとに item-box で区切って、その中からリンク・名前・価格・画像を拾う
    for block in res.text.split('class="item-box"')[1:]:
        m = _RAKUMA_LINK.search(block)
        if not m:
            continue
        name = re.search(r'item-box__item-name.*?<span[^>]*>(.*?)</span>', block, re.S)
        price = re.search(r'data-content="(\d+)"', block)
        img = re.search(r'(?:data-original|src)="(https://[^"]*fril\.jp/img/[^"]+)"', block)
        if "soldout" in block.split("item-box__item-name")[0]:
            continue
        out.append({
            "site": "ラクマ",
            "id": "r:" + m.group(2),
            "title": unescape(re.sub(r"<[^>]+>", "", name.group(1))).strip() if name else "",
            "price": int(price.group(1)) if price else 0,
            "thumb": img.group(1) if img else None,
            "url": m.group(1),
        })
    return out


# ---------------- 判定 ----------------
def pattern_score(img: Image.Image) -> float:
    """ボルドー地の割合と、青い花の割合から 0〜1 のスコアを出す。"""
    w, h = img.size
    img = img.convert("RGB").crop((int(w * .2), int(h * .2), int(w * .8), int(h * .8)))
    x = np.asarray(img.resize((96, 96)), dtype=np.float32) / 255.0
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    mx, mn = x.max(-1), x.min(-1)
    v, d = mx, mx - mn
    s = np.where(mx > 0, d / np.maximum(mx, 1e-6), 0)
    hue = np.zeros_like(mx)
    nz = d > 1e-6
    rm = nz & (mx == r); gm = nz & (mx == g) & ~rm; bm = nz & ~rm & ~gm
    hue[rm] = ((g - b)[rm] / d[rm]) % 6
    hue[gm] = (b - r)[gm] / d[gm] + 2
    hue[bm] = (r - g)[bm] / d[bm] + 4
    hue *= 60
    wine = ((hue >= 270) | (hue <= 20)) & (s > 0.12) & (v > 0.06) & (v < 0.45)
    # 花の青〜ラベンダー。白は背景の壁や床と区別できないので数えない
    blue = (hue >= 200) & (hue < 290) & (s > 0.10) & (v > 0.30)
    a = min(wine.mean() / 0.08, 1.0)
    bfl = min(blue.mean() / 0.10, 1.0)
    return float(np.sqrt(a * bfl))


def classify(item):
    t = item["title"].lower()
    if not any(k.lower() in t for k in CONFIG["must_any"]):
        return None, 0.0
    if not any(k.lower() in t for k in CONFIG["item_any"]):
        return None, 0.0
    if any(k.lower() in t for k in CONFIG["exclude"]):
        return None, 0.0
    if any(k.lower() in t for k in CONFIG["strong_keywords"]):
        return "強一致", 1.0
    score = 0.0
    if item["thumb"]:
        try:
            r = S.get(item["thumb"], timeout=20)
            r.raise_for_status()
            score = pattern_score(Image.open(io.BytesIO(r.content)))
        except Exception as e:  # 画像が取れなくても監視は止めない
            print(f"thumb error {item['url']}: {e}", file=sys.stderr)
    if score >= CONFIG["color_threshold"]:
        return "候補", score
    # 型（type J juliet）が同じなら配色の条件をゆるめる
    if any(k.lower() in t for k in CONFIG["shape_keywords"]) and score >= CONFIG["shape_color_threshold"]:
        return "型一致", score
    return None, score


# ---------------- 通知（メール） ----------------
def send_mail(hits):
    """hits: [(item, label, score)] を1通のメールにまとめて送る。"""
    lines, html = [], []
    for it, label, score in hits:
        extra = f"（配色スコア {score:.2f}）" if label != "強一致" else ""
        lines.append(f"[{label}] {it['site']} ¥{it['price']:,} {it['title']}{extra}\n{it['url']}")
        img = f'<img src="{escape(it["thumb"])}" width="240"><br>' if it["thumb"] else ""
        html.append(f'<p><b>[{label}] {escape(it["site"])} ¥{it["price"]:,}</b>{escape(extra)}<br>'
                    f'<a href="{escape(it["url"])}">{escape(it["title"])}</a><br>{img}</p>')
    if os.environ.get("DRY_RUN") or not os.environ.get("SMTP_USER"):
        print("[メール]\n" + "\n".join(lines))
        return
    msg = EmailMessage()
    strong = any(l == "強一致" for _, l, _ in hits)
    msg["Subject"] = f"stina {'強一致' if strong else '候補'} {len(hits)}件の新着"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ.get("MAIL_TO") or os.environ["SMTP_USER"]
    msg.set_content("\n\n".join(lines))
    msg.add_alternative("<html><body>" + "".join(html) + "</body></html>", subtype="html")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465")), timeout=30) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        smtp.send_message(msg)


def main():
    if os.environ.get("TEST_MAIL"):
        send_mail([({"site": "テスト", "price": 0, "title": "stina-watcher のテスト送信です",
                     "url": "https://github.com/madoris-plus/stina-watcher", "thumb": None}, "強一致", 1.0)])
        print("テストメールを送信しました")
        return
    seen = set(json.loads(SEEN_PATH.read_text())) if SEEN_PATH.exists() else set()
    seed = bool(os.environ.get("SEED")) or not SEEN_PATH.exists()
    evaluate = bool(os.environ.get("EVALUATE"))  # 既読を無視して判定だけ表示する
    if evaluate:
        seen, seed = set(), True
    items, errors = {}, []
    for kw in CONFIG["keywords"]:
        for name, fn in (("mercari", search_mercari), ("rakuma", search_rakuma)):
            try:
                for it in fn(kw):
                    items.setdefault(it["id"], it)
            except Exception as e:
                errors.append(f"{name} '{kw}': {e}")
            time.sleep(2)
    for e in errors:
        print("ERROR", e, file=sys.stderr)

    new = [it for i, it in items.items() if i not in seen]
    print(f"取得 {len(items)} 件 / 新着 {len(new)} 件" + (" (初回: 通知なしで既読化)" if seed else ""))
    hits = []
    for it in new:
        label, score = classify(it)
        if label:
            hits.append((it, label, score))
            print(f"  {label} {score:.2f} {it['site']} {it['title']} {it['url']}")
        elif os.environ.get("DRY_RUN"):
            print(f"  skip {score:.2f} {it['site']} {it['title']}")
    # 初回は今出ている分を既読にするだけ（一覧はログに残る）
    if hits and not seed:
        send_mail(hits)
    if not evaluate:
        SEEN_PATH.write_text(json.dumps(sorted(seen | set(items)), ensure_ascii=False, indent=0))
    # 両サイトとも全滅なら失敗扱いにして気づけるようにする
    if errors and not items:
        sys.exit(1)


if __name__ == "__main__":
    main()
