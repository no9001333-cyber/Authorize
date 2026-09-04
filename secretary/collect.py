#!/usr/bin/env python3
"""비서실장 일일 브리핑 파이프라인.

이 저장소(REPO)의 이슈/PR을 업무 항목으로 수집하고, 우선순위와
"확인·결정 필요" 여부를 판단한 뒤 data/dashboard.json과 정적 대시보드
(docs/index.html, GitHub Pages용)를 생성한다.

필요 환경변수:
  REPO               owner/repo (기본값: GITHUB_REPOSITORY)
  GITHUB_TOKEN       이슈/PR 조회용 (Actions 기본 토큰으로 충분)
  ANTHROPIC_API_KEY  (선택) 있으면 Claude가 우선순위/확인필요 여부를 판단.
                     없으면 라벨·키워드 기반 휴리스틱으로 대체한다.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

ARCHIVE_AFTER_DAYS = 7          # 완료 후 7일 지나면 목록에서 제외 (질문 13-A)
UPCOMING_WINDOW_DAYS = 14

URGENT_LABELS = {"urgent", "critical", "긴급", "p0", "p1"}
DECISION_LABELS = {"decision-needed", "needs-decision", "승인", "결재"}
DECISION_KEYWORDS = ("승인", "결재", "확정", "계약", "발송")
PROGRESS_LABELS = {"in-progress", "wip", "진행중"}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def gh_api(path):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_issues():
    items, page = [], 1
    while True:
        batch = gh_api(f"/repos/{REPO}/issues?state=all&per_page=100&page={page}")
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


def classify_with_claude(title, body):
    prompt = (
        "다음 GitHub 이슈를 보고 JSON으로만 답하라.\n"
        '형식: {"priority":"high|mid|low","needsDecision":true|false,"note":"한 문장 메모"}\n'
        "priority는 마감 임박도와 영향도를 기준으로 판단하라.\n"
        "needsDecision은 금전·계약·발송 등 되돌리기 어려운 결정이 필요한 경우에만 true로 하라.\n\n"
        f"제목: {title}\n본문: {(body or '')[:1500]}"
    )
    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    text = data["content"][0]["text"]
    start, end = text.find("{"), text.rfind("}") + 1
    return json.loads(text[start:end])


def classify_heuristic(labels, title):
    label_set = {l.lower() for l in labels}
    priority = "high" if label_set & URGENT_LABELS else "mid"
    needs_decision = bool(label_set & DECISION_LABELS) or any(k in title for k in DECISION_KEYWORDS)
    return {"priority": priority, "needsDecision": needs_decision, "note": ""}


def to_task(issue):
    labels = [l["name"] if isinstance(l, dict) else l for l in issue.get("labels", [])]
    label_set = {l.lower() for l in labels}
    is_pr = "pull_request" in issue
    closed_at = issue.get("closed_at")

    if issue["state"] == "closed":
        status = "done"
    elif label_set & PROGRESS_LABELS:
        status = "progress"
    else:
        status = "pending"

    if status == "done" and closed_at:
        closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - closed_dt > timedelta(days=ARCHIVE_AFTER_DAYS):
            return None  # 완료 7일 경과 -> 아카이브(목록 제외)

    try:
        classification = (
            classify_with_claude(issue["title"], issue.get("body"))
            if ANTHROPIC_API_KEY
            else classify_heuristic(labels, issue["title"])
        )
    except Exception as e:
        print(f"[warn] 분류 실패 (#{issue['number']}): {e}", file=sys.stderr)
        classification = classify_heuristic(labels, issue["title"])

    milestone = issue.get("milestone") or {}
    deadline = (milestone.get("due_on") or "")[:10] or None

    return {
        "id": f"gh-{issue['number']}",
        "title": issue["title"],
        "category": "PR" if is_pr else "이슈",
        "priority": classification.get("priority", "mid"),
        "status": status,
        "deadline": deadline,
        "needsDecision": bool(classification.get("needsDecision")),
        "note": classification.get("note") or "",
        "sourceUrl": issue["html_url"],
        "updatedAt": issue.get("updated_at"),
    }


def dday_info(deadline):
    if not deadline:
        return {"text": "마감없음", "soon": False, "diff": None}
    d = datetime.fromisoformat(deadline).date()
    t = datetime.now(timezone.utc).date()
    diff = (d - t).days
    text = "D-DAY" if diff == 0 else (f"D-{diff}" if diff > 0 else f"D+{abs(diff)}")
    return {"text": text, "soon": diff <= 7, "diff": diff}


PRIORITY_LABEL = {"high": "상", "mid": "중", "low": "하"}
STATUS_LABEL = {"pending": "대기", "progress": "진행", "done": "완료"}


def esc(s):
    return (
        (s or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def render_card(task):
    dd = dday_info(task["deadline"])
    soon_class = " soon" if dd["soon"] and task["status"] != "done" else ""
    flag = '<span class="decision-flag">확인 필요</span>' if task["needsDecision"] else ""
    return f"""
      <a class="task-card" href="{esc(task['sourceUrl'])}" target="_blank" rel="noopener">
        <div class="card-top">
          <span class="tag">{esc(task['category'])}</span>
          <span class="priority {task['priority']}">우선순위 {PRIORITY_LABEL.get(task['priority'], '중')}</span>
        </div>
        <div class="card-title">{esc(task['title'])}</div>
        <div class="card-bottom">
          <span class="dday{soon_class}">{dd['text']}</span>
          {flag}
        </div>
      </a>"""


def render_html(tasks, repo, generated_at, artifact_url):
    cols = {"pending": [], "progress": [], "done": []}
    for t in tasks:
        cols.setdefault(t["status"], []).append(t)
    order = {"high": 0, "mid": 1, "low": 2}
    for k in cols:
        cols[k].sort(key=lambda t: (order.get(t["priority"], 1), t["deadline"] or "9999-99-99"))

    decisions = [t for t in tasks if t["needsDecision"] and t["status"] != "done"]
    upcoming = sorted(
        (t for t in tasks if t["status"] != "done" and t["deadline"] and dday_info(t["deadline"])["diff"] <= UPCOMING_WINDOW_DAYS),
        key=lambda t: dday_info(t["deadline"])["diff"],
    )
    urgent_count = sum(1 for t in tasks if t["status"] != "done" and t["deadline"] and dday_info(t["deadline"])["soon"])

    def column_html(status):
        items = cols.get(status, [])
        if not items:
            return '<p class="empty-note">항목 없음</p>'
        return "".join(render_card(t) for t in items)

    def decisions_html():
        if not decisions:
            return '<p class="empty-note">확인이 필요한 항목이 없습니다.</p>'
        items = []
        for t in decisions:
            note_html = f'<span class="li-note">{esc(t["note"])}</span>' if t["note"] else ""
            items.append(f'<li><span class="li-title">{esc(t["title"])}</span>{note_html}</li>')
        return "".join(items)

    def upcoming_html():
        if not upcoming:
            return '<p class="empty-note">임박한 마감이 없습니다.</p>'
        return "".join(
            f'<li><span class="li-title mono">{dday_info(t["deadline"])["text"]}</span> · {esc(t["title"])}</li>'
            for t in upcoming
        )

    summary = f"대기 {len(cols['pending'])}건 · 진행 {len(cols['progress'])}건 · 완료 {len(cols['done'])}건"
    if decisions:
        summary += f" · 확인 필요 {len(decisions)}건"

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>비서실 현황판 · {esc(repo)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@700;900&family=Noto+Sans+KR:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
  :root{{
    --bg:#f5f2ea; --surface:#ffffff; --surface-2:#eee9db; --ink:#20232e; --ink-soft:#4b5166;
    --muted:#767c8c; --border:#ddd7c6; --accent:#2b3a67; --brass:#a5722a;
    --done:#2f7a4e; --done-soft:#e1f0e6; --progress:#b5691f; --progress-soft:#fbeada;
    --pending:#686e82; --pending-soft:#e9e9ee; --urgent:#a83f30; --brass-soft:#f4e9d6;
    --shadow:rgba(33,37,47,0.08);
  }}
  @media (prefers-color-scheme: dark){{
    :root:not([data-theme="light"]){{
      --bg:#12151c; --surface:#1b202b; --surface-2:#1f2531; --ink:#e9e7df; --ink-soft:#c7c9d6;
      --muted:#9aa1b4; --border:#2c3242; --accent:#8b9bdb; --brass:#d9a75c;
      --done:#6fc08c; --done-soft:#1c3025; --progress:#e3a15c; --progress-soft:#3a2a18;
      --pending:#a7adbe; --pending-soft:#252a37; --urgent:#e28277; --brass-soft:#3a2f1e;
      --shadow:rgba(0,0,0,0.45);
    }}
  }}
  :root[data-theme="dark"]{{
    --bg:#12151c; --surface:#1b202b; --surface-2:#1f2531; --ink:#e9e7df; --ink-soft:#c7c9d6;
    --muted:#9aa1b4; --border:#2c3242; --accent:#8b9bdb; --brass:#d9a75c;
    --done:#6fc08c; --done-soft:#1c3025; --progress:#e3a15c; --progress-soft:#3a2a18;
    --pending:#a7adbe; --pending-soft:#252a37; --urgent:#e28277; --brass-soft:#3a2f1e;
    --shadow:rgba(0,0,0,0.45);
  }}
  *{{box-sizing:border-box;}}
  body{{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans KR",sans-serif;font-variant-numeric:tabular-nums;}}
  h1,h2,h3{{font-family:"Noto Serif KR",serif;margin:0;text-wrap:balance;}}
  .mono{{font-family:"JetBrains Mono",monospace;}}
  .topbar{{max-width:1200px;margin:0 auto;padding:32px 24px 20px;}}
  .eyebrow{{margin:0 0 6px;font-family:"JetBrains Mono",monospace;font-size:11px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase;}}
  .topbar h1{{font-size:28px;font-weight:900;color:var(--accent);}}
  .summary{{margin:10px 0 0;font-size:15px;color:var(--ink-soft);}}
  .note-banner{{max-width:1200px;margin:0 auto;padding:0 24px 12px;font-size:12.5px;color:var(--muted);}}
  .note-banner a{{color:var(--accent);}}
  .kpis{{max-width:1200px;margin:0 auto;padding:0 24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;}}
  .kpi{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;box-shadow:0 1px 2px var(--shadow);}}
  .kpi .num{{font-family:"JetBrains Mono",monospace;font-size:26px;font-weight:500;}}
  .kpi .lbl{{font-size:12.5px;color:var(--muted);margin-top:2px;}}
  .kpi.pending .num{{color:var(--pending);}} .kpi.progress .num{{color:var(--progress);}}
  .kpi.done .num{{color:var(--done);}} .kpi.urgent .num{{color:var(--urgent);}}
  .layout{{max-width:1200px;margin:20px auto 60px;padding:0 24px;display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:20px;align-items:start;}}
  @media (max-width:880px){{.layout{{grid-template-columns:1fr;}}}}
  .board{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;}}
  @media (max-width:640px){{.board{{grid-template-columns:1fr;}}}}
  .column{{background:var(--surface-2);border-radius:14px;padding:14px;min-height:120px;display:flex;flex-direction:column;gap:10px;}}
  .column-head{{display:flex;align-items:center;justify-content:space-between;padding:0 2px 4px;}}
  .column-head h3{{font-size:14px;font-weight:700;font-family:"Noto Sans KR",sans-serif;}}
  .count-pill{{font-family:"JetBrains Mono",monospace;font-size:12px;border-radius:20px;padding:2px 9px;font-weight:500;}}
  .column[data-status="pending"] .count-pill{{background:var(--pending-soft);color:var(--pending);}}
  .column[data-status="progress"] .count-pill{{background:var(--progress-soft);color:var(--progress);}}
  .column[data-status="done"] .count-pill{{background:var(--done-soft);color:var(--done);}}
  .task-card{{display:flex;flex-direction:column;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 13px;box-shadow:0 1px 2px var(--shadow);text-decoration:none;color:inherit;transition:transform .12s ease,box-shadow .12s ease;}}
  .task-card:hover{{transform:translateY(-2px);box-shadow:0 4px 10px var(--shadow);}}
  .card-top{{display:flex;align-items:center;justify-content:space-between;gap:8px;}}
  .tag{{font-family:"JetBrains Mono",monospace;font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);}}
  .priority{{font-size:11.5px;font-weight:500;}}
  .priority.high{{color:var(--urgent);}} .priority.mid{{color:var(--brass);}} .priority.low{{color:var(--muted);}}
  .card-title{{font-size:14.5px;font-weight:500;line-height:1.4;}}
  .card-bottom{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}}
  .dday{{font-family:"JetBrains Mono",monospace;font-size:11.5px;color:var(--muted);}}
  .dday.soon{{color:var(--urgent);font-weight:500;}}
  .decision-flag{{font-size:10.5px;background:var(--brass-soft);color:var(--brass);border-radius:6px;padding:2px 6px;font-weight:500;}}
  .empty-note{{color:var(--muted);font-size:12.5px;padding:8px 2px;margin:0;}}
  .panel{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px;margin-bottom:14px;box-shadow:0 1px 2px var(--shadow);}}
  .panel h2{{font-size:14.5px;font-weight:700;font-family:"Noto Sans KR",sans-serif;margin-bottom:10px;}}
  .panel ul{{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px;}}
  .panel li{{font-size:13px;border-left:3px solid var(--brass);padding:4px 0 4px 10px;}}
  .panel.actions li{{border-left-color:var(--accent);}}
  .panel li .li-title{{font-weight:500;}}
  .panel li .li-note{{display:block;color:var(--muted);font-size:11.5px;margin-top:2px;}}
  footer{{max-width:1200px;margin:0 auto;padding:0 24px 40px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;}}
</style>
</head>
<body>
<header class="topbar">
  <p class="eyebrow">SECRETARIAT STATUS BOARD (GITHUB) · {esc(repo)}</p>
  <h1>비서실 현황판</h1>
  <p class="summary">{esc(summary)}</p>
</header>
<p class="note-banner">이 페이지는 GitHub Actions가 매일 이슈·PR을 집계해 생성하는 읽기 전용 보기입니다.
등록·수정이 가능한 실시간 화면은 <a href="{esc(artifact_url)}" target="_blank" rel="noopener">비서실 현황판(아티팩트)</a>에서 이용하세요.</p>
<section class="kpis">
  <div class="kpi pending"><div class="num">{len(cols['pending'])}</div><div class="lbl">대기중</div></div>
  <div class="kpi progress"><div class="num">{len(cols['progress'])}</div><div class="lbl">진행중</div></div>
  <div class="kpi done"><div class="num">{len(cols['done'])}</div><div class="lbl">완료</div></div>
  <div class="kpi urgent"><div class="num">{urgent_count}</div><div class="lbl">마감 임박 (7일 이내)</div></div>
</section>
<main class="layout">
  <div class="board">
    <div class="column" data-status="pending">
      <div class="column-head"><h3>대기</h3><span class="count-pill">{len(cols['pending'])}</span></div>
      {column_html('pending')}
    </div>
    <div class="column" data-status="progress">
      <div class="column-head"><h3>진행</h3><span class="count-pill">{len(cols['progress'])}</span></div>
      {column_html('progress')}
    </div>
    <div class="column" data-status="done">
      <div class="column-head"><h3>완료</h3><span class="count-pill">{len(cols['done'])}</span></div>
      {column_html('done')}
    </div>
  </div>
  <aside class="side">
    <div class="panel">
      <h2>확인·결정 필요</h2>
      <ul>{decisions_html()}</ul>
    </div>
    <div class="panel actions">
      <h2>다음 액션 / 마감 임박</h2>
      <ul>{upcoming_html()}</ul>
    </div>
  </aside>
</main>
<footer>
  <span>마지막 갱신 {esc(generated_at)}</span>
  <span>매일 07:00 KST(평일) GitHub Actions가 자동 갱신합니다.</span>
</footer>
</body>
</html>"""


def main():
    if not REPO:
        print("REPO(GITHUB_REPOSITORY) 환경변수가 없습니다.", file=sys.stderr)
        sys.exit(1)

    issues = fetch_issues()
    tasks = [t for t in (to_task(i) for i in issues) if t is not None]

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    artifact_url = os.environ.get(
        "ARTIFACT_URL",
        "https://claude.ai/code/artifact/c6cf95a4-28d1-411d-b8ca-283f5ef3a488",
    )

    os.makedirs(os.path.join(REPO_ROOT, "data"), exist_ok=True)
    os.makedirs(os.path.join(REPO_ROOT, "docs"), exist_ok=True)

    with open(os.path.join(REPO_ROOT, "data", "dashboard.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"repo": REPO, "generatedAt": generated_at, "tasks": tasks},
            f, ensure_ascii=False, indent=2,
        )

    html = render_html(tasks, REPO, generated_at, artifact_url)
    with open(os.path.join(REPO_ROOT, "docs", "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    print(f"{len(tasks)}건 수집 완료 · data/dashboard.json, docs/index.html 생성")


if __name__ == "__main__":
    main()
