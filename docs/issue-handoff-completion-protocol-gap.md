# Issue: Worker "completion" 프로토콜에 공식 리뷰 요청 단계가 강제되지 않는다

**Reported:** 2026-04-21  
**First observed:** first-shovel-ride-score-contract-fix handoff (fs-worker-1-first-shovel-ride-score-contra 세션)  
**Prior context:** first-shovel-cleanup 세션에서는 우연히 잘 동작, ride-score 세션에서 재현

---

## 1. 현상

Implementation 워커가:

1. 할당된 handoff 작업(코드 수정 + 테스트 + alembic migration 등)을 모두 완료
2. "내부 QA 보고서"를 본인 채팅 스트림에 작성 (pytest/coverage 등 evidence 포함)
3. **그 지점에서 멈춤.**
   - `orchctl handoff claim --by <peer>` 미실행
   - `orchctl handoff complete ...` 미실행
   - `claude-peers send_message(to_id=CTO, ...)` 미발송
4. 워커가 idle 상태로 진입. CTO 쪽에서는 시스템 레벨(YAML)에서 여전히 `status: open`

CTO가 tmux `send-keys`로 수동 nudge를 해야만 워커가 orchctl 호출을 돌리고 공식 complete 상태로 전환.

---

## 2. 근본 원인

워커 bootstrap artifact (예: `.orchestrator/runtime/bootstrap/fs-worker-1-first-shovel-ride-score-contra.md` L108~L118)의 팀장 프로토콜이 다음과 같이 끝난다:

> ### Reporting to CTO  
> When the handoff is complete, report using this structure:  
> …  
> Do NOT forward raw sub-agent output to CTO. Curate and summarize.  
> *This is an internal QA report. Official handoff review is pending CTO/reviewer decision.*

즉 프로토콜이 **"내부 QA 보고서를 쓰고 CTO가 리뷰를 결정할 때까지 기다려라"**로 종결됨. 다음 두 가지가 **명문화되지 않음**:

1. `orchctl handoff claim + complete` 자동 호출 의무
2. `claude-peers send_message`로 CTO에게 "리뷰 요청" 브로드캐스트 의무

Escalate 항목(L100~L103)은 "blocker / design decision 필요 / scope 오류"에만 국한. 정상 완료 케이스는 어떤 채널로 알릴지 전혀 규정되지 않음.

### 2.1 왜 cleanup 세션에서는 잘 동작했는가

cleanup 워커는 자발적으로 `send_message`를 추가 호출한 경우. 프로토콜이 강제한 게 아니라 워커의 성실한 재량. ride-score 워커는 프로토콜 문언만 엄격히 이행하여 멈춤. → **동일 프로토콜에서도 워커별 편차 발생.**

### 2.2 피어 채널 기술적 문제는 아님

- `claude-peers list_peers(scope="machine")`로 CTO 세션(pts/18)과 워커 세션(pts/5) 서로 탐지됨
- CTO 세션이 `set_summary`로 "CTO 세션 — review 요청 여기로" 태그를 설치해두면 워커가 machine scope 리스트에서 매칭 가능
- 따라서 메시지 발송 경로 자체는 정상. 원인은 워커가 발송 로직을 안 돌린 것.

---

## 3. 영향

- CTO가 워커를 자주 모니터링하지 않으면 handoff 체인이 정체됨 (cleanup → ride-score → auth 순차 진행 시 체감)
- tmux `send-keys` 기반 수동 nudge가 사실상의 복구 수단이 됨 → 운영자 개입 부하 증가
- Opus xhigh 쿼타가 idle 동안 낭비될 가능성 (워커가 컨텍스트 유지 + 메시지 대기)

---

## 4. 개선 후보

### 4.1 Bootstrap 템플릿 강화 (우선순위: 높음)

팀장 프로토콜 말미에 **반드시 다음 3단계를 순서대로 수행**하라는 의무 조항 추가:

```
When the internal QA report is complete AND the task acceptance criteria are satisfied:

1. `orchctl handoff claim --by <your peer_id>`  (unless already claimed)
2. `orchctl handoff complete --by <your peer_id> --summary "..." --validation-cover "1:..." --task-criterion-cover "1:..." ...`
   — Include every validation and task criterion with evidence.
3. `claude-peers send_message(to_id=<CTO peer_id>, message="[<your peer_id> → CTO] <handoff-id> COMPLETE. Summary: ...")`
   — If CTO peer_id unknown, run `list_peers(scope="machine")` first and match on summary containing "CTO".

Do NOT stop until step 3 is confirmed sent.
```

### 4.2 CTO peer 자동 탐색 규약

- CTO 세션은 세션 시작 시 `set_summary("CTO — <room-id> room 운영 중. handoff complete 후 peers로 리뷰 요청 바랍니다.")`
- 워커는 `list_peers`에서 summary가 "CTO"로 시작하는 peer에게 메시지 발송
- 또는 orchctl에 `cto-peer` 메타 설정을 두고 bootstrap에 주입

### 4.3 Post-complete hook (orchctl 쪽)

`orchctl handoff complete`가 호출되면:
- 기존 wiki-suggest hook처럼
- 자동으로 CTO peer에게 `claude-peers send_message`로 알림 발송
- 워커가 3단계를 빠뜨려도 시스템이 보완

기술적으로 handoff complete 명령 안에서 peers API 호출을 추가하거나, 별도 daemon이 runtime/handoffs 변경을 watch해서 트리거.

### 4.4 Idle 상태 메시지 수신 개선

이 이슈와 별개로, 워커가 idle 상태에 들어간 후 peers 메시지가 도착해도 즉시 처리 안 되는 현상이 bidcompass 때부터 재현 중. 현재 우회는 tmux `send-keys`. 구조적 해결:

- workers' chat loop이 peers message arrival을 idle 중에도 polling
- 또는 channel notification을 직접 받아 루프 재개

---

## 5. 일단의 운영 규칙 (개선 전까지)

- 새 세션에 handoff dispatch 직후 **CTO가 bootstrap의 "Reporting to CTO" 섹션을 읽었다는 걸 명시적으로 확인**하도록 tmux send-keys로 추가 지시 넣기
- 예: "내부 QA 보고 작성 후 반드시 orchctl handoff claim + complete 호출 후 claude-peers send_message로 CTO(peer_id: <id>)에게 리뷰 요청 전송해라"
- 이 문장을 handoff create 시 `--constraint`로 넣으면 워커가 bootstrap에서 자동 인지

---

## 6. 후속 작업

- [x] bootstrap 템플릿 수정 — 팀장 프로토콜 말미에 3단계 의무 조항 추가 (4.1)
- [ ] CTO peer 자동 탐색 규약 문서화 (4.2)
- [ ] post-complete hook 설계 및 구현 (4.3)
- [ ] idle 상태 peer 메시지 수신 이슈 재현 케이스 수집 (4.4)
- [ ] first-shovel-catchup room의 후속 handoff(auth/create/profile/flutter-play)에서 4.1 임시 우회 constraint 적용 테스트

### 2026-04-25 Update

Implemented the first guardrail in code:

- `lib/bootstrap.py` now includes an **Official Completion Handshake** section
  in every generated worker bootstrap.
- `lib/handoffs.py` now includes the same official completion steps in
  dispatch brief verification expectations.
- Regression coverage lives in `tests/test_completion_protocol.py`.

This does not yet implement a daemon, tmux screen scraper, or automatic
post-complete peer notification. The next safe step is still either CTO peer
auto-discovery or a best-effort post-complete notification hook.
