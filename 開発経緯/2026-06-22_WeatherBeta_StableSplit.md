# 2026-06-22 気象データbeta / stable-beta分離 / Beta HTTPS化

## 開発者の要望
1. KPIテーブルにTemperature/Pressure/Humidity/Air Densityの4列を追加（FP_start時刻で補間）
2. いきなり最新版を更新したくない → 安定版(stable)とbeta版を別URLで運用したい
3. Betaも HTTPSにしたい

## 実装内容

### 1. KPIデフォルトソート（FP_start降順）
- KPIページのDataTablesデフォルトソートをFP_start降順（新しい順）に変更
- 実装: kpi.html の `order: [[fp_start_col_idx, "desc"]]`

### 2. 気象データカラム（Beta機能）
**データソース**
- RDS `weather_sensor` テーブル（約286万件、1分間隔）
- カラム: `temperature`, `pressure`, `humidity`(0〜1の小数), `density`, `created_at`(JST)
- 注意: `utc_timestamp` カラム名がMySQL予約語と衝突 → `created_at` を使用

**実装（utils.py `get_weather_for_timestamps()`）**
- 指定時間範囲の気象レコードをクエリ → DataFrameに変換
- 各ラップのFP_start時刻に対して線形補間
- humidity は 0.54 → 54.0% に変換（×100）
- density は小数点5桁で表示（例: 1.12426）

**表示（kpi.html）**
- 4列をKPIテーブルの最右に追加（Temp(℃) / Press(hPa) / Humid(%) / Density）
- 列ヘッダに β バッジ（`<span class="badge bg-secondary">β</span>`）
- `render_template` に `weather_beta_cols` を渡してヘッダ識別

### 3. stable/beta 分離

**Gitブランチ戦略**
- `stable` ブランチ: `4e15b02`（デコーダチェック追加まで）をHEADとして維持
- `main` ブランチ: beta最新機能（ソート + 気象データ）
- git worktree: `../20260430_MyLapsWebApps_stable/` にstableをチェックアウト（ローカル2ポート同時起動用）

**EC2 2アプリ構成**
| 項目 | stable | beta |
|------|--------|------|
| ディレクトリ | `/home/ec2-user/webapp/` | `/home/ec2-user/webapp_beta/` |
| systemdサービス | `webapp.service` | `webapp_beta.service` |
| gunicorn port | 127.0.0.1:8000 | 127.0.0.1:8001 |
| nginx listen | 443 (HTTPS) | 5003 (HTTPS) |
| venv | `/home/ec2-user/webapp/venv/` | シンボリックリンク（共用） |
| デフォルトport(app.py) | 5002 | 5003 |

**EC2セキュリティグループ**
- `sg-0f44ab26e552c3ad8` にTCP 5003 (0.0.0.0/0) を追加（AWS CLI）

### 4. Beta HTTPS化
- `webapp_beta.conf` を `listen 5003` → `listen 5003 ssl` に変更
- 証明書: stableと同じ自己署名証明書を流用
  - `/etc/pki/tls/certs/webapp-selfsigned.crt`
  - `/etc/pki/tls/private/webapp-selfsigned.key`
- `X-Forwarded-Proto $scheme` ヘッダ追加

## URL
- Stable: `https://ec2-43-206-155-252.ap-northeast-1.compute.amazonaws.com/`
- Beta: `https://ec2-43-206-155-252.ap-northeast-1.compute.amazonaws.com:5003`
  - ブラウザ警告が出るが自己署名のため正常。「詳細設定→続行」で入れる

## 注意事項
- betaのvenvはwebapp/venvへのシンボリックリンクなので、パッケージ追加時は両方に影響する
- EC2の `settings.json`（DB認証情報）はwebapp_betaにも別途コピーが必要（git管理外）

## Jamieへの連絡
- Timing System Discussionスレッドにreply-all（CC: robert.hanson@japanhpc.com）でドラフト作成
- 内容: FP_startソート + 気象データbeta + BetaURL + フィードバック依頼

## コミット
- `c45dd1f` : Beta default port 5003 変更
- `cc1c745` : 気象データカラム追加

## 開発者FB
- EC2 beta HTTPS動作確認済み（2026-06-22）
- Jamieへのメールはユーザーが自分でドラフト修正・送信
