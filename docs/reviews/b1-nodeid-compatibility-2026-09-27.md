# B1 시험 식별자 호환성 — focused 한정 인수

2026-09-27. 판정 **APPROVE_METADATA_FOCUSED_ONLY**. 최종 feature 통합·전체 시험·
실제 B1 전체 프로필 또는 운영 승인이 아니다.

## Plan

기존 OS 계약 시험의65537-byte 공백 입력이 자동 parameter 이름으로 확장돼 하나의
node ID가65647bytes였다. producer의2048-byte 제한을 넘는다. 두 계약 모듈의
기존109개 원시 수집 결과를 보존하고, 별도 Astra/xhigh 계획 리뷰 후 딱 한 줄만
`pytest.param(b" " * (64 * 1024 + 1), id="document-too-large")`로 표기했다.
입력·5개 case·assertion·producer/guard/제한은 바꾸지 않았다.

## Do → See

- 별도 worktree/branch: `b1-nodeid-metadata-20260927` /
  `feature/b1-nodeid-metadata-20260927`.
- Base2fc2531, 정확한 후보 `82e107f9f43c72607fcb436d8834712f384753a0`.
  테스트 파일 한 줄만 변경했고 정적 diff·비밀 패턴·보존 검사를 통과했다.
- 기존 raw의2048-byte predicate는 예상대로 exit1이다. 과거 원문 분석이며 pytest
  call-phase RED나 새 실행 실패 예외로 표현하지 않는다.
- root의 기존 격리 UTC300 collection:109개(OS73/base36),0.17s·격리0·세 exit0.
  독립 literal 대조에서 원래108개 ID 불변, 지정한 한 개만 변경, 누락/중복/예상 밖0,
  최대178bytes. 다른 파일이나 향후 전체 suite의 호환성을 추정하지 않는다.
- 두 전체 선택 모듈의 실제 호출:109 passed/0.52s·격리0·workload/tee/tool exit0.
  같은 입력65537bytes를 원래 assertion으로 계속 검사한다. 재시도/제외/완화 없음.
- 비작성자 Astra/xhigh가 정확한 diff·원문·109개 매핑·지문을 직접 대조해 지적0으로
  focused 한정 승인했다. 실제 모델/effective effort metadata는 미노출·unverified다.

변경한 OS 시험 SHA256:
`533fe854a575c292332a2437b93a832a70039246891cf05de31f7c3d8c7cdf87`.
canonical sorted109-node JSON SHA256:
`bac07f5170d2c5e514228aa87099bd0946129427f33e12e8d45a70e71fc7a36e`.
전체 원문·명령/exit 연결·지문·독립 리뷰는
`.superpowers/sdd/2026-09-27-b1-standard-runner/nodeid-compatibility-*`와
`nodeid-fresh-collection.log`, `nodeid-focused-82e107f.log`에 보존한다.

## 다음 인수 조건

독립 승인된 완성 runner와 기존 B1b를 합친 **새 동결 후보**에서 readiness 검토,
변경하지 않은36-node 실제 작은 B1 smoke, worker 전원 종료 후 root 단독의 기존
legacy UTC900/KST900 전체 검증, 최종 독립 리뷰를 모두 거쳐야 최종 통합한다.
그 전에는 별도 branch/focused 상태를 유지한다. 과거 전체 결과를 새 후보의 결과로
전용하지 않는다. 향후 full-profile expected inventory도 새 후보에서 별도로 수집·결속한다.

이 변경은 모든 suite ID, B1 receipt 생산, 출력/시간 예산, N4097, source/native,
semantic 결과 또는 CI/운영 준비를 입증하지 않는다. main·배포·재시작·주문·전략·
위험·계좌/자격증명·설정은 변경하지 않았다.
