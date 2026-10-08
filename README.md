# stina-watcher

stina のレオタード「innocent bordeaux」柄（ボルドー地に青白の花柄）が
メルカリ・ラクマに新しく出品されたら、メールで通知します。

## 通知の種類
- **強一致**: タイトルに innocent / ボルドー などの柄名を含む
- **型一致**: type J juliet で、配色もある程度近い
- **候補**: stina のレオタードで、サムネイルの配色が近い（ボルドー地＋青い花）
- 1回の実行で見つかった分を1通にまとめ、画像と商品リンク付きで届きます

## 設定（GitHub Actions で20分ごとに実行）
1. 送信用 Gmail で2段階認証を有効にし、アプリパスワードを発行（Googleアカウント → セキュリティ → アプリパスワード）
2. このフォルダを GitHub のリポジトリに置く
3. リポジトリの Settings → Secrets and variables → Actions に登録
   - `SMTP_USER`: 送信用 Gmail アドレス
   - `SMTP_PASS`: アプリパスワード（16文字）
   - `MAIL_TO`: 受け取るアドレス（送信用と同じなら不要）
4. Actions タブで stina-watch を一度手動実行（初回は今ある出品を既読にするだけ）

## 調整
`config.json`
- `keywords`: 検索語
- `strong_keywords`: これを含むと必ず通知
- `shape_keywords`: 型名。配色が `shape_color_threshold` 以上なら通知
- `exclude`: キッズ・小物など除外語
- `color_threshold`: 配色判定のしきい値（下げると通知が増える）

判定だけ試す: Actions → stina-watch → Run workflow で evaluate にチェック（メール・既読保存なし）
