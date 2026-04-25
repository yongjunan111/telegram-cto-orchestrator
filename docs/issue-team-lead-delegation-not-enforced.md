# Issue: Opus team lead delegation이 강제되지 않아 직접 구현으로 흘러간다

**Reported:** 2026-04-25  
**First observed:** `orch-room-idle-snapshot-v1` handoff  
**Related context:** Idle Snapshot V1, worker auto-launch, team lead bootstrap protocol

---

## 1. 현상

의도한 실행 모델은 다음과 같다:

1. CTO가 큰 handoff를 만든다.
2. Opus 기반 team lead 세션이 handoff를 읽는다.
3. Team lead는 작업을 작은 단위로 쪼갠다.
4. 쪼개진 sub-task는 Sonnet/Haiku 같은 더 가벼운 모델에게 위임한다.
5. Team lead는 결과를 수거하고 검증한 뒤 CTO에게 요약 보고한다.

하지만 실제 `orch-room-idle-snapshot-v1`에서는:

1. CTO가 handoff를 만들고 Worker D에게 dispatch했다.
2. Worker D 세션은 `worker.model: opus` 설정으로 실행됐다.
3. Bootstrap에는 team lead protocol과 sub-handoff 규칙이 포함됐다.
4. 그러나 Worker D가 하위 sub-agent를 만들지 않고 직접 구현했다.
5. 결과적으로 공식 기록에는 CTO handoff와 worker completion은 남았지만,
   team lead가 만든 별도 sub-handoff ledger는 남지 않았다.

즉, Opus 세션은 생성됐지만 "Opus는 머리, Sonnet/Haiku는 손발"이라는
운영 의도가 시스템적으로 보장되지 않았다.

---

## 2. 확인된 사실

- `worker_launch.py`는 `.orchestrator/config.yaml`의 `worker.model`을 읽고
  Claude CLI에 `--model <value>`를 붙인다.
- 현재 config에는 `worker.model: opus`가 설정되어 있다.
- 따라서 이번 문제는 "Opus team lead 세션이 안 떴다"가 아니다.
- Bootstrap에는 "You are a team lead" 및 sub-handoff protocol이 포함된다.
- 동시에 bootstrap과 `docs/sub-handoff-format.md`는 "default is direct execution"과
  "작고 집중된 작업은 직접 하라"는 규칙도 포함한다.
- Sub-handoff는 Claude Code Agent tool 프롬프트 규약으로만 정의되어 있고,
  `.orchestrator/handoffs/*.yaml`에 공식 child handoff로 자동 기록되지 않는다.
- `handoff complete`는 subtask ledger나 child handoff evidence를 요구하지 않는다.

---

## 3. 근본 원인

### 3.1 Delegation이 프롬프트 권고에 머문다

현재 시스템은 team lead에게 "필요하면 쪼개라"고 말한다.
하지만 handoff YAML, completion gate, CLI validator 어디에도
"이 작업은 반드시 위임해야 한다"는 hard rule이 없다.

모델은 자신이 해결 가능하다고 판단하면 직접 구현하는 쪽으로 흐른다.
특히 Opus는 문제 해결 능력이 높기 때문에, 비용 절감 관점과 반대로
"그냥 내가 처리"하는 선택을 하기 쉽다.

### 3.2 Team lead와 worker 역할이 같은 peer에 섞여 있다

`orch-worker-d-gc-audit` 같은 peer가 동시에:

- team lead 역할을 받고,
- 구현 작업도 할 수 있고,
- 최종 completion도 올릴 수 있다.

즉, "팀장은 직접 구현 금지"라는 권한 경계가 없다.

### 3.3 Sub-handoff가 공식 상태로 남지 않는다

Sub-handoff format 문서는 있지만, 이 문서는 Claude Code Agent tool에 넣을
structured prompt 형식이다.

그 결과 team lead가 실제로 하위 agent를 썼더라도, 별도 저장 규칙이 없으면
오케스트레이터 상태에는 다음이 남지 않는다:

- child handoff id
- child assignee/model
- child owned files
- child acceptance coverage
- child verification evidence
- team lead의 accept/rework/escalate ledger

### 3.4 Completion gate가 delegation evidence를 요구하지 않는다

`delegate_required` 같은 실행 정책이 없고, `handoff complete`도 child evidence를
검사하지 않는다.

따라서 team lead가 직접 구현해도 공식 completion은 성공한다.

---

## 4. 영향

- Opus가 구현까지 맡으면서 모델 비용 구조가 의도와 달라진다.
- 작은 작업을 작은 모델에 맡기는 실험이 검증되지 않는다.
- CTO 입장에서는 "team lead가 진짜로 나눠 맡겼는지"를 기록만 보고 알기 어렵다.
- Subtask별 책임 범위, 테스트 증거, 재작업 기록이 남지 않는다.
- 나중에 ultrareview나 PR 리뷰를 붙일 때도 어떤 하위 작업이 어떤 판단으로
  나뉘었는지 추적하기 어렵다.

---

## 5. 개선 후보

### 5.1 Handoff execution policy 추가

Handoff task에 실행 정책을 명시한다:

```yaml
execution:
  mode: direct | delegate_optional | delegate_required
```

- `direct`: 직접 구현 허용
- `delegate_optional`: team lead가 판단해서 직접 구현 또는 위임
- `delegate_required`: team lead 직접 구현 금지, child handoff evidence 필요

### 5.2 `delegate_required` hard gate

`delegate_required` handoff에서는 `handoff complete` 전에 다음을 요구한다:

- 최소 1개 이상의 child handoff 또는 subtask ledger
- 각 child task의 owned files
- parent acceptance/validation criterion과 child evidence 매핑
- team lead의 accept/rework/escalate 결과
- 직접 파일 수정 금지 위반 여부 확인

이 조건이 없으면 completion을 fail-closed 처리한다.

### 5.3 Team lead peer type 분리

Peer registry에 team lead 역할을 명시한다:

```yaml
type: team_lead
can_edit_files: false
can_delegate: true
can_complete_parent: true
```

일반 worker와 team lead를 분리하면 "비싼 모델은 판단, 싼 모델은 실행"이라는
운영 의도를 권한 모델로 표현할 수 있다.

### 5.4 공식 child handoff 기록

Claude Agent tool 내부 프롬프트만으로 끝내지 말고, 오케스트레이터 상태에도
child handoff를 남긴다.

후보 구조:

```yaml
handoff:
  id: parent-id
execution:
  mode: delegate_required
  child_handoffs:
    - id: parent-id-sub-1
      model_target: sonnet
      owned_files:
        - lib/foo.py
      status: completed
      outcome: accepted
```

또는 별도 `.orchestrator/subhandoffs/*.yaml`을 두는 방식도 가능하다.

### 5.5 Model routing 명시

Subtask마다 목표 모델을 기록한다:

```yaml
model_target: sonnet | haiku | opus
```

Team lead는 기본적으로 Opus를 쓰되, child task는 Sonnet/Haiku가 기본값이 되게 한다.
Opus child task는 설계 판단이나 고위험 리뷰처럼 예외 상황에만 허용한다.

---

## 6. 임시 운영 규칙

구현 전까지 delegation이 꼭 필요한 handoff에는 task constraints에 아래 문장을 넣는다:

```text
This handoff is delegate_required. As team lead, do not implement directly.
Create structured sub-handoffs for disjoint implementation/test/doc slices,
assign them to lighter model workers where possible, collect evidence, and
include a subtask ledger in the final completion report. If no safe decomposition
exists, escalate to CTO instead of implementing directly.
```

또한 CTO는 dispatch 후 worker bootstrap에 이 문구가 들어갔는지 확인한다.

---

## 7. 후속 작업

- [ ] Handoff schema에 `execution.mode` 추가
- [ ] `delegate_required` completion gate 설계
- [ ] Team lead peer type과 권한 모델 설계
- [ ] 공식 child handoff 또는 subtask ledger 저장 위치 결정
- [ ] Subtask model routing 규칙 문서화
- [x] Bootstrap에서 `delegate_required`일 때 "직접 구현 금지" 문구를 강하게 렌더링
- [ ] Regression test: `delegate_required`인데 child evidence 없이 complete하면 실패
- [ ] Regression test: `direct` handoff는 기존처럼 complete 가능

---

## 8. 현재 결론

이번 문제는 모델 선택 실패가 아니다.

Opus team lead 세션은 설정상 정상적으로 뜬다. 다만 delegation이 프롬프트
권고에 머물고, completion gate가 subtask evidence를 요구하지 않기 때문에,
Opus가 직접 구현해도 시스템이 막지 못한다.

따라서 "Opus는 team lead, Sonnet/Haiku는 executor"라는 비용/역할 분리 목표를
달성하려면 `delegate_required` 같은 hard policy와 completion gate가 필요하다.

---

## 9. V1 Implementation Note

V1 implementation lands across two coordinated handoffs:

- `orch-delegation-gate-schema` — handoff schema gains an `execution`
  block with `mode in {direct, delegate_optional, delegate_required}` and
  a `child_handoffs` list. The completion gate uses `execution.mode` to
  decide whether child evidence is required. This is the schema/gate side.
- `orch-session-cleanup-policy-v1` — bootstrap rendering reads
  `execution.mode` and prepends a hard `DELEGATION REQUIRED — DO NOT
  IMPLEMENT DIRECTLY` block when the mode is `delegate_required`. Every
  bootstrap (regardless of mode) gains a `POST-COMPLETE CLEANUP` block
  telling the worker to file a checkpoint and surface a pane marker, but
  forbidding self-kill of its own tmux session. Also lands the read-only
  `lib/session_cleanup.py` report and `docs/session-cleanup-policy.md`.

Together these two handoffs deliver the V1 hard delegation policy. The
remaining checkboxes above (peer type separation, child handoff storage
location, subtask model routing, regression tests on the gate) are
deferred to follow-up handoffs and are not blocked by this V1 ship.
