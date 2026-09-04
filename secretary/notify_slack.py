#!/usr/bin/env python3
"""비서실장 일일 브리핑 결과를 Slack으로 요약 발송한다.

SLACK_WEBHOOK_URL이 설정되어 있지 않으면 조용히 종료한다(선택 기능).
표준 보고 형식(한줄요약 / 처리완료 / 확인·결정 필요 / 다음 액션)을 따른다.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
JOB_STATUS = os.environ.get("JOB_STATUS", "unknown")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dashboard():
    path = os.path.join(REPO_ROOT, "data", "dashboard.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_message():
    if JOB_STATUS not in ("success", "unknown"):
        return (
            f":warning: 비서실장 실행 실패 (status: {JOB_STATUS})\n"
            "GitHub Actions 로그를 확인해 주세요."
        )

    data = load_dashboard()
    if not data:
        return ":warning: 비서실장: 대시보드 데이터를 찾을 수 없습니다."

    tasks = data["tasks"]
    pending = [t for t in tasks if t["status"] == "pending"]
    progress = [t for t in tasks if t["status"] == "progress"]
    done = [t for t in tasks if t["status"] == "done"]
    decisions = [t for t in tasks if t["needsDecision"] and t["status"] != "done"]

    lines = [
        f"*비서실장 일일 브리핑* ({data['generatedAt']})",
        f"대기 {len(pending)}건 · 진행 {len(progress)}건 · 완료 {len(done)}건"
        + (f" · 확인 필요 {len(decisions)}건" if decisions else ""),
    ]
    if decisions:
        lines.append("\n*확인·결정 필요*")
        for t in decisions[:5]:
            lines.append(f"• <{t['sourceUrl']}|{t['title']}>")
    lines.append(f"\n대시보드: {os.environ.get('DASHBOARD_URL', '')}".rstrip())
    return "\n".join(lines)


def main():
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL이 설정되어 있지 않아 알림을 건너뜁니다.")
        return
    text = build_message()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps({"text": text}).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[warn] Slack 발송 실패: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
