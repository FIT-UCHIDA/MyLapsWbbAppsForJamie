# CLAUDE.md — Lap Time Analyzer Web App

## プロジェクト概要
競輪ラップタイム分析 KPI表示 Webアプリケーション（Flask + Bootstrap 5 + DataTables.js）

## EC2デプロイ手順

EC2にアップロードする際は、必ず以下の手順を踏むこと。

### 1. EC2の設定ファイルをバックアップ（EC2側）
```bash
ssh -i "<PEM>" ec2-user@ec2-43-206-155-252.ap-northeast-1.compute.amazonaws.com \
  "cd /home/ec2-user/webapp && mkdir -p backups && TS=\$(date +%Y%m%d_%H%M%S) && \
   for f in settings.json users_config.json kpi.json; do \
     if [ -f \$f ]; then cp \$f backups/\${f%.json}_\${TS}.json; fi; done"
```

### 2. EC2の設定ファイルをローカルにアーカイブ
```bash
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p ec2_archives/${TS}
scp -i "<PEM>" ec2-user@<HOST>:/home/ec2-user/webapp/settings.json \
  ec2-user@<HOST>:/home/ec2-user/webapp/users_config.json \
  ec2-user@<HOST>:/home/ec2-user/webapp/kpi.json \
  ec2_archives/${TS}/
scp -r -i "<PEM>" ec2-user@<HOST>:/home/ec2-user/webapp/users/ ec2_archives/${TS}/
```

### 3. SCP で新ファイルをアップロード
```bash
scp -i "<PEM>" webapp/app.py webapp/version.json ec2-user@<HOST>:/home/ec2-user/webapp/
scp -r -i "<PEM>" webapp/templates/ ec2-user@<HOST>:/home/ec2-user/webapp/
scp -r -i "<PEM>" webapp/static/ ec2-user@<HOST>:/home/ec2-user/webapp/
```

### 4. サービス再起動
```bash
ssh -i "<PEM>" ec2-user@<HOST> "sudo systemctl restart webapp"
```

### 5. version.json 更新
デプロイ後、`webapp/version.json` を最新の git commit hash で更新し、EC2にもアップロードすること。

### 6. Git commit + push
変更を commit し、GitHub (origin main) に push すること。
コミット前に「〇〇の変更 yyyymmddHHMM のバージョンをコミットしますがいいですか？」と確認すること。

## 接続情報
- **PEM**: `FIT_MyLaps_WebApp_for_Jamie.pem`（プロジェクトルート）
- **EC2 Host**: `ec2-43-206-155-252.ap-northeast-1.compute.amazonaws.com`
- **EC2 User**: `ec2-user`
- **App Path**: `/home/ec2-user/webapp/`
- **Services**: `webapp.service`（gunicorn）+ `nginx`
- **GitHub**: `https://github.com/FIT-UCHIDA/MyLapsWbbAppsForJamie.git`

## ファイル構成ルール

### Git管理対象
- `webapp/app.py`, `webapp/utils.py`, `webapp/kpi.json`
- `webapp/templates/*.html`, `webapp/static/**`
- `webapp/requirements.txt`, `webapp/Procfile`, `webapp/version.json`
- `.gitignore`, `CLAUDE.md`, `開発経緯.md`

### Git除外（.gitignore）
- `webapp/settings.json` — DB認証情報を含む
- `webapp/users/` — ユーザー個別データ
- `webapp/users_config.json` — ユーザー有効/無効状態
- `webapp/backups/` — 自動バックアップ
- `ec2_archives/` — EC2設定ファイルのローカルアーカイブ
- `*.pem` — SSH秘密鍵
- `Key` — 認証情報

## バックアップシステム
- KPI/設定ファイルの保存・アップロード時にタイムスタンプ付きバックアップが自動作成される
- 保存先: 元ファイルと同じディレクトリの `backups/` サブフォルダ
- 最大50件保持、古いものから自動削除

## 開発ルール
- `開発経緯.md` に開発者の要望、Claudeの対策、結果のFBを時刻付きで記録する
- 定期的に Git にコミットし、コミット前にユーザーに確認を取る
