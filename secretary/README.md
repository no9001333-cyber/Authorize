# 비서실장 일일 브리핑

`.github/workflows/secretary-daily.yml`이 평일 07:00 KST에 `secretary/collect.py`를
실행해 이 저장소의 이슈·PR을 업무 항목으로 모으고, `data/dashboard.json`과
정적 대시보드 `docs/index.html`을 갱신·커밋한다. 이후 `secretary/notify_slack.py`가
Slack으로 요약을 보낸다.

## 처음 설정할 것

1. **GitHub Pages 켜기**: 저장소 Settings → Pages → Source를
   `Deploy from a branch` / `main` / `/docs`로 지정하면
   `https://<owner>.github.io/<repo>/`에서 정적 대시보드를 볼 수 있다.
2. **(선택) Secrets 등록** — 저장소 Settings → Secrets and variables → Actions
   - `ANTHROPIC_API_KEY`: 있으면 Claude가 각 이슈의 우선순위·확인필요 여부를
     내용 기반으로 판단한다. 없으면 라벨/키워드 휴리스틱으로 대체되어
     동작 자체는 문제없지만 판단 정교함이 떨어진다.
   - `SLACK_WEBHOOK_URL`: Slack Incoming Webhook URL. 있으면 매일 요약과
     실행 실패 알림을 해당 채널로 보낸다. 없으면 알림 단계는 조용히 건너뛴다.
3. **수동 실행 테스트**: Actions 탭 → "비서실장 일일 브리핑" → Run workflow로
   즉시 1회 실행해 정상 동작을 확인할 수 있다(`workflow_dispatch`).

## 이슈 라벨 규칙

| 라벨(대소문자 무관) | 의미 |
|---|---|
| `in-progress`, `wip`, `진행중` | 상태를 "진행"으로 표시 |
| `urgent`, `critical`, `긴급`, `p0`, `p1` | 우선순위 "상" (휴리스틱 모드에서만 사용) |
| `decision-needed`, `needs-decision`, `승인`, `결재` | "확인·결정 필요"로 표시 |

마일스톤에 마감일(due date)을 설정하면 대시보드의 "마감(D-day)"에 반영된다.
완료(closed) 후 7일이 지난 항목은 자동으로 목록에서 제외된다(영구 삭제는 아님 — GitHub 이슈 자체는 남는다).

## 알아둘 점 (범위 제한)

- **이메일·캘린더는 이 파이프라인에 포함되어 있지 않다.** GitHub Actions
  러너는 Gmail/Google Calendar에 대한 OAuth 인증을 기본으로 갖고 있지 않아,
  이를 연동하려면 별도로 Google Cloud 서비스 계정 또는 OAuth refresh token을
  발급받아 GitHub Secret으로 등록하고 `secretary/collect.py`에 수집 로직을
  추가해야 한다. 현재는 "이 저장소의 이슈·PR"만 업무 소스로 삼는다(질문 8-A).
- **claude.ai 아티팩트(비서실 현황판)는 이 워크플로가 직접 갱신하지 않는다.**
  아티팩트의 실시간 DB는 브라우저에서 페이지가 열려 있을 때만 쓸 수 있는
  구조라 GitHub Actions에서 직접 쓸 수 없다. 대신 매일 GitHub Actions 실행
  약 20분 뒤 별도 Claude Code Remote Routine("비서실장 GitHub→아티팩트 동기화",
  trig_015pPnRMkpbpyrmzuztgytQn)이 `data/dashboard.json`을 읽어 GitHub 이슈
  유래 항목(`gh-`로 시작하는 id)만 아티팩트 DB의 `tasks` 컬렉션에 반영한다.
  사용자가 아티팩트 화면에서 직접 추가한 업무는 건드리지 않는다.
- 대시보드 데이터 스키마는 `data/dashboard.json`의 `tasks[]`를 참고.
  (`id, title, category, priority, status, deadline, needsDecision, note, sourceUrl, updatedAt`)
