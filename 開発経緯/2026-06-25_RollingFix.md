# 2026-06-25 Rollingエフォートバグ修正

## 経緯
Jamie（HPCJC）から「ローリングエフォートのタイムがおかしい」と報告。
6/25午後に同様のセッションがあるため緊急対応。

## バグの原因
- Rollingエフォート（BPスタート・125m刻み）では、前のセットの最終ラップが0mを踏まずに終わる
- 次のセットの最初の「BP進入」通過が、前ラップの`split_laps`処理中に取り込まれる（遷移ラップ）
- 結果：FP_startより早いBP通過 → 負の区間タイムが発生

## 修正内容

### utils.py — `split_laps()` 後処理追加
```python
# Rolling effort 修正:
# ローリングスタート(BP始まり)の「遷移ラップ」で、FP_start より前の通過記録が
# 混入しマイナス時刻になるのを除去する。
if "FP_start" in result_df.columns:
    fp_col = pd.to_datetime(result_df["FP_start"])
    for col in ["60m", "AP1", "50m", "100m", "BP", "150m", "AP2", "200m"]:
        if col in result_df.columns:
            mask = pd.to_datetime(result_df[col]) < fp_col
            result_df.loc[mask, col] = pd.NaT
```

### kpi.json — `rolling`モード追加（EC2のみ）
```json
"rolling": {
    "mainKPI": "FP_start-BP",
    "intervals": [
        {"start": "FP_start", "end": "BP",       "name": "FP→BP (125m)"},
        {"start": "BP",       "end": "FP_start",  "name": "BP→FP (125m)"}
    ]
}
```
EC2の既存モード（flying/standing/training3/training4）に追加。

## 確認結果
6/24 15:10〜16:20（10選手、Rolling3セット）のデータで検証：
- 負の値：**ゼロ**（修正OK）
- FP→BP / BP→FP の典型値：5〜25秒（125m競輪スプリント速度として妥当）
- 残課題：セット間休憩が遷移ラップのFP→BPに混入（数十分〜30分程度の大きな値）→ 別途対応

## デプロイ
- EC2へSCP: utils.py 2026-06-25 08:24 UTC
- kpi.json: Python経由でEC2上直接更新
- サービス再起動: webapp + webapp_beta
- ベータ版（port 5003）で動作確認

## 運用ルール（新規）
- **改善・テストはベータ版（port 5003 HTTPS）で先行**
- 安定動作確認後にstable（port 443）へ反映
- EC2の安定版コードは `/home/ec2-user/webapp_stable/` ディレクトリで管理（予定）

## 注意事項
- 遷移ラップ（セット間）の大きな値は現在フィルタなし
- Rolling最初の125m（BP開始前のFP通過なし）は構造的に取得不可
- EC2のkpi.jsonはローカルと構成が異なる（EC2: flying/standing/rolling/training3/training4、ローカル: training1-4/rolling）
