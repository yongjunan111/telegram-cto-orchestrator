# Architecture Decision Records (ADR)

This document records major architectural decisions and the approaches we tried, measured, and abandoned on the way to the current design.

Authoritative truth remains code, `.orchestrator/*.yaml`, and git history. This file is design rationale, not state.

---

### 🗑️ Abandoned Approaches & Trade-offs (도입 후 폐기한 접근 방식들)

#### 1. LLM Auto-write to Wiki → Suggest-only + Cycle-gated + Fingerprint-split ownership

- **초기 도입 목적 (Initial Intent):** Karpathy의 LLM Wiki 패턴을 그대로 가져와, 핸드오프 사이클이 끝날 때마다 LLM이 `decisions.md`/`lessons.md`/`patterns.md`를 자동 요약·갱신하도록 설계. RAG를 대체하는 "compiled knowledge layer"를 저비용으로 누적하는 게 목적.
- **치명적 문제 및 폐기 이유 (Failure & Bottleneck):**
  - (a) Auto-generated dump degradation
  - (b) False-positive dedupe
  - (c) Standalone task pollution
  - (d) Parent-op coupling risk
- **최종 아키텍처 결론 (Final Decision):** `lib/wiki_suggest.py`로 분리하되
  - (i) 쓰기 권한 박탈
  - (ii) Cycle-gate
  - (iii) Fingerprint write/read 권한 분리
  - (iv) Best-effort 격리로 선회

#### 2. Aggressive Session Reuse & Tmux-as-Source-of-Truth → Fresh-per-handoff + Exact Pane Targeting + O_EXCL Lock + CAS Revalidation

- **초기 도입 목적 (Initial Intent):** 워커 프로세스 기동 비용과 컨텍스트 재로드 비용을 아끼기 위해 idle tmux 세션을 공격적으로 재사용.
- **치명적 문제 및 프로덕션 결함 (Failure & Bottleneck):**
  - (a) Context pollution
  - (b) Double-claim race (TOCTOU)
  - (c) Silent command misrouting
  - (d) Stale binding as evidence
  - (e) Parse-error fresh fallback
- **최종 아키텍처 결론 (Final Decision):** Fresh-per-handoff를 default로 선회.
  - (i) 정확한 pane id(`%N`) 타게팅
  - (ii) per-session O_EXCL lock file + CAS revalidation
  - (iii) Dead-tmux binding skip/warning
  - (iv) fail-closed 처리

#### 3. Stored Derived Summary in Authoritative State → Render-time Derivation + "Completed ≠ Approved" Split

- **초기 도입 목적 (Initial Intent):** `room show` 렌더링을 매번 재계산하기 싫어서, 활성 handoff 요약 등을 room state YAML에 미리 저장해두는 캐시 전략. `handoff.status=completed`를 사실상 "승인됨"으로 취급.
- **치명적 문제 및 폐기 이유 (Failure & Bottleneck):**
  - (a) Silent stale UI
  - (b) Rendered contradiction
  - (c) Review ceremony (고무도장화)
  - (d) Parse-error 은폐
- **최종 아키텍처 결론 (Final Decision):**
  - (i) Stored-vs-derived 엄격 분리 (렌더 시점에 YAML에서 재계산)
  - (ii) Execution/Review 축 분리
  - (iii) Review authority separation
  - (iv) Rework = new handoff + structured `must_address` delta

---

> 💡 **공통 엔지니어링 테제:** "조용한 복구(silent recovery)는 금지, 조용한 저하(silent degradation)는 금지, 파생물(derived)은 진실(authoritative)이 될 수 없다."

---

### 🚧 Locked Roadmap Boundaries (향후 범위 고정)

이 섹션은 폐기한 접근의 회고가 아니라, **현재 시점에 의도적으로 잠가둔 미래 로드맵 경계**를 기록한다. 각 항목은 "V1에서는 하지 않는다"와 "V2/V3 이후로만 가능하다"를 명시적으로 분리한다.

#### 1. Room-Level GC Audit — V1/V2/V3 Boundary Lock

- **결정 배경 (Why):** Idle-Triggered Context GC(유휴 세션 자동 아카이브)는 세션 손실·오판 시 폭발반경이 크므로, 자동화부터 도입하면 복구 불가능한 사고로 직결된다. [Debate 001 — Room-Level GC Audit](agent-debate-records.md#debate-001-room-level-gc-audit)의 `APPROVE WITH CHANGES` 결정에 따라, 동일 기능을 **V1(관찰) → V2(명시적 1회성 아카이브) → V3+(자동화)**의 세 단계로 강제 분리한다. 세 층은 서로 다른 시간, 서로 다른 커밋, 서로 다른 승인 경로에 속한다.

- **V1 — Read-Only Promotion Audit (`orchctl room gc-audit <room-id>`):**
  - (a) V1은 **진단 전용**이며 어떤 상태도 변경하지 않는다:
    - room/handoff/review/session YAML 쓰기 금지
    - `.orchestrator/wiki/` 하위 문서 쓰기 금지
    - tmux 세션/pane kill·respawn·send-keys 금지
    - 프로바이더 컨텍스트 변형(`/compact`, 세션 리셋 등) 호출 금지
  - (b) 판단 신호는 **Tier 1 authoritative YAML + git**만 사용한다. tmux/process/pane liveness 등 Tier 2 runtime observation은 리포트에 표시만 하고 `audit_verdict`에는 영향을 주지 않는다.
  - (c) V1 리포트는 **아카이브 준비 스텁을 포함하지 않는다**. 구체적으로 다음은 모두 금지:
    - V2 archive-ready/dry-run payload를 리포트에 미리 채워두는 것
    - 리포트 최상위에 `archive_ready`, `green_light`, `ready_for_archive`, `auto_archive_eligible` 같은 "아카이브 해도 됨" 총괄 플래그 필드를 내보내는 것
    - 사용자/워커가 리포트만 읽고 "이제 자동으로 아카이브가 돌아간다"로 해석할 여지가 있는 문구
  - (d) `audit_verdict` 값은 `promoted | at-risk | unbound | parse-error`로 고정한다. `stale_tmux`는 at-risk 사유가 아니다 — runtime_observation에만 기록한다.
  - (e) **V1 리포트는 V2 트리거가 아니다.** 리포트 파일이 존재한다는 사실 자체는 어떤 아카이브 권한도 부여하지 않는다.

- **V2 — Explicit Session Archive (`orchctl session archive <session-id> --from-report <report-path>`):**
  - (a) V2는 **명시적·1회성 명령**으로만 호출된다. cron/hook/idle timer가 자동으로 호출해서는 안 된다.
  - (b) `--from-report` 경로는 **반드시 절대경로(absolute path)** 여야 한다. 상대경로는 거부한다. 이유:
    - 상대경로는 호출자 CWD에 따라 다른 파일을 읽어 TOCTOU/혼동을 유발한다
    - 리포트 파일을 의도한 실체와 다르게 지정하면 다른 세션을 잘못 아카이브할 위험이 있다
    - 레포 경계를 넘는 심볼릭 링크/상대 트래버설 우회를 차단하기 쉬워진다
  - (c) V2는 리포트를 **재검증**한다: 리포트에 기록된 YAML 해시/게시 상태/git layout이 *호출 시점 현재 값*과 일치해야 한다. 불일치 시 아카이브를 수행하지 않고 `stale_report`로 실패한다.
  - (d) V2는 리포트의 `audit_verdict`가 `promoted`인 세션에 대해서만 아카이브를 수행한다. `at-risk`/`unbound`/`parse-error`는 거부한다.
  - (e) V2는 V1이 하지 않는 runtime 재검증(tmux 생사, 프로세스 존재 등)을 호출 시점에 수행한다 — 단, 이는 V2 시점의 재확인이지 V1 리포트 재구성이 아니다.

- **V3+ — Automatic Idle Cleanup (로드맵만 기록, 현시점 미구현):**
  - (a) V3는 `last_active_time` 임계 초과 세션을 대상으로 **자동 V1 감사 → 자동 V2 아카이브**를 연결하는 단계다.
  - (b) V3는 **V1과 V2가 실전에서 안정화된 이후에만** 착수한다. 안정화 기준(최소): V1/V2 각각 복수 실사용 사이클 무사고, at-risk 분류의 false-positive/false-negative 케이스 기록 누적, 운영자의 수동 아카이브 경험.
  - (c) V3 이전에는 어떤 idle timer/hook도 세션을 자동으로 아카이브하지 않는다.
  - (d) V3에 들어가더라도 자동 경로는 **revert/undo 가능한 형태**(격리 이동 + 보존 기간)로 설계해야 하며, 즉시 하드 삭제는 금지한다.

- **의도적 비보장 항목 (Explicit Non-Goals at this boundary):**
  - V1 리포트만을 근거로 V2/V3가 자동 실행되도록 연결하지 않는다 — 현시점에서 "V1 report → auto archive"는 아키텍처 위반이다.
  - V1에서 wiki-suggest/ADR 자동 생성 훅을 걸지 않는다 — 이는 별도의 suggest-only 레일을 따른다.
  - V2는 리포트 없이 호출할 수 없다. `--from-report`를 생략하거나 우회하는 모드(`--force`, `--best-effort` 등)를 현 단계에서 추가하지 않는다.

---

> 🚧 **로드맵 분리 테제:** "관찰(V1)과 변경(V2)과 자동화(V3)는 서로 다른 시간, 서로 다른 커밋, 서로 다른 승인 경로에 속한다. 하나의 PR/리포트가 세 층을 동시에 건드리면 가장 위험한 쪽의 실패 반경으로 묶인다."
