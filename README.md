# Question Studio Astra

문제 이미지 크롭, 원본 기반 전사, 정답·해설 연결, 문제은행 관리, 문서 편집과 PDF/HWPX 출력용 **Windows 로컬 앱**입니다.

## 공개 스냅샷

- 소스 스냅샷: **2026-10-09 문항 생성 대체 출제·사후 검토 변경 전** 운영 소스. 패키지 버전은 `0.1.0`입니다.
- PC/모바일 화면, 정답-only 전사, 시험지 영역 편집, 문제은행·문서·PDF/HWPX, 같은 PC 숙제 앱 읽기 연동 및 현재 문항 생성 메뉴를 포함합니다.
- 아직 운영에 반영되지 않은 중복 검사·물리 과목 max 정책·Claude 한도 소진 대체·사후 교차 검토는 이 변경 전 스냅샷에 포함하지 않았습니다.
- 공개 사본에서는 개인 이름과 개인 Tailscale 호스트를 예시 값으로 치환했습니다. 운영 작업 트리와 운영 Git 이력은 복사하지 않습니다.

## 포함하지 않는 자료

저장 문항과 학생 정보, 원본 PDF/이미지, 사용자 문서, 배부·학습 이력, DB, API 키·로그인 토큰, 브라우저 프로필, 실행 환경, 작업 결과·실패 기록, 평가 원문 및 `.omo` 내부 자료는 포함하지 않습니다. 코드에 포함된 예제와 회귀 테스트는 별도 가상 자료이며 운영 문제은행을 대신하지 않습니다.

## 실행 환경과 범위

- Python **3.11 이상** 및 프로젝트 가상환경이 필요합니다. Python 의존성은 `pyproject.toml`과 `uv.lock`을 참고하세요.
- 문서·크롭 웹 의존성은 `studio/web/package.json`, `crop/web/package.json`에 정의되어 있습니다. 문서 편집기의 현재 번들과 필요한 글꼴·고지를 함께 보존합니다.
- 프로젝트의 일반 진입점은 `launch_astra.py`입니다. `--help`로 인자를 확인하고, 운영 경로 대신 새 작업공간과 별도 포트를 사용하세요.
- `Open-Astra.ps1` / `.cmd`는 기존 Windows 소유 런타임용 진입점입니다. 가상환경 및 Python 런타임 경로를 확인하지 않고 다른 PC에서 동일하게 동작한다고 가정하지 마세요.
- `studio/README.md`, `crop/README.md`의 일부 기존 경로·포트 안내는 역사적 자료입니다. 루트 런처와 현재 설정이 우선입니다.
- 이 공개 갱신은 호스팅 서비스 배포나 새로운 PC의 전체 설치 검증을 의미하지 않습니다. 원본 문제를 전사하려면 사용할 제공자의 별도 인증과 권한이 필요합니다.

## 같은 PC 숙제 앱 연동

계약: [`studio/docs/integrations/homework-local-v1.md`](studio/docs/integrations/homework-local-v1.md).

`/api/integrations/homework/local/v1` 아래에서 문항 목록, revision에 결박된 문항 상세, revision/SHA에 결박된 참조 자산을 GET으로 읽습니다. 운영 토큰을 공유하지 않습니다. `X-Astra-Local-Client: homework`는 **비밀 인증값이 아니며 같은 PC 프로세스를 서로 격리하지 않습니다**. 브라우저·외부 프록시 접근과 쓰기는 차단합니다. 학생 기록과 숙제 생성 앱은 이 저장소의 역할이 아닙니다.

## 검증

현재 공개 사본에서 확인한 결과는 [`docs/PUBLIC_SNAPSHOT_VERIFICATION.md`](docs/PUBLIC_SNAPSHOT_VERIFICATION.md)에 기록합니다. 운영 OCR·API 쓰기·저장 문항을 검증용으로 사용하지 않습니다.

## 제3자 코드와 라이선스

저장소를 공개로 볼 수 있다는 사실이 전체 코드의 MIT 라이선스나 자유로운 재배포 허가를 뜻하지 않습니다. 기존 저작권 고지와 각 구성요소의 허가 범위를 유지합니다.

- [`studio/THIRD_PARTY_NOTICES.md`](studio/THIRD_PARTY_NOTICES.md)
- [`studio/EXAM_REGION_UPSTREAM.md`](studio/EXAM_REGION_UPSTREAM.md)
- [`studio/licenses/hwp-eqn-ts-MIT.txt`](studio/licenses/hwp-eqn-ts-MIT.txt)
- [`studio/web/vendor/LICENSE.katex`](studio/web/vendor/LICENSE.katex)
- [`studio/web/vendor/document/THIRD-PARTY-NOTICES.txt`](studio/web/vendor/document/THIRD-PARTY-NOTICES.txt) 및 글꼴 OFL 고지
- [`studio/web/vendor/document/KaTeX-Fonts-OFL-1.1.txt`](studio/web/vendor/document/KaTeX-Fonts-OFL-1.1.txt): KaTeX/MathLive 글꼴 저작권·예약 이름·OFL 전문
- [`studio/docs/SOURCE_MANIFEST.json`](studio/docs/SOURCE_MANIFEST.json): 경로를 익명화한 코드 출처 및 공개 재배포 허가 확인 범위

PNKmath 부분은 게시자가 **공개 GitHub 재배포까지 원저작자에게 허가받았다고 확인한 범위**로 포함합니다. 기록된 별도 허가를 임의의 오픈소스 라이선스나 제3자에 대한 무제한 재배포 허가로 바꾸지 않습니다.
