# soccer-predictor

EPL / K리그1 / K리그2 / 분데스리가 승무패(1X2) 예측 개인 프로젝트.

## 로컬 실행

```
pip install -r requirements.txt

# 1) 데이터 수집 (예: EPL 최근 시즌)
python scraper/run_scrape.py --league EPL --season 2025-2026

# 2) 백테스트로 모델 점검
python model/backtest.py --league EPL

# 3) API 서버
uvicorn api.main:app --reload --port 8000

# 4) 프론트엔드 (별도 터미널)
cd web
npm install
npm run dev
```

## 데이터 출처
- 경기 결과 + 1X2 배당: betexplorer.com (개인용 분석 목적, 요청 간 딜레이를 두어 서버 부하 최소화)

## 배포 (GitHub Actions + Render + Vercel + Turso)

로컬 실행은 위 단계 그대로면 되고, Turso 환경변수가 없으면 항상 로컬 `data/soccer.db`를 씀(계정 없이도 개발 가능). 실제로 폰에서 접속 가능하게 배포하려면 아래 순서대로 진행.

### 1. GitHub 레포 생성 + push
GitHub에서 새 레포를 만든 뒤:
```
git init
git add .
git commit -m "Initial commit"
git remote add origin <레포 URL>
git push -u origin main
```

### 2. Turso (무료 호스팅 DB)
1. https://turso.tech 가입 → 새 데이터베이스 생성
2. `turso db show <db이름> --url` 로 `TURSO_DATABASE_URL`, `turso db tokens create <db이름>` 로 `TURSO_AUTH_TOKEN` 발급 (또는 대시보드에서 확인)
3. 기존 로컬 데이터를 옮기려면:
   ```
   # PowerShell
   $env:TURSO_DATABASE_URL = "libsql://..."
   $env:TURSO_AUTH_TOKEN = "..."
   python migrate_to_turso.py
   ```
4. GitHub 레포 Settings → Secrets and variables → Actions에 `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` 등록 (`.github/workflows/scrape.yml`이 매일 자동으로 이 값을 사용해 데이터 갱신)

### 3. Render (백엔드)
1. https://render.com 가입 → New → Blueprint → 방금 push한 GitHub 레포 선택 (`render.yaml` 자동 인식)
2. 환경변수에 `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` 입력 후 배포
3. 배포 완료되면 나오는 URL(`https://soccer-predictor-api.onrender.com` 형태) 확인 → `curl <URL>/leagues`로 정상 응답 확인

### 4. Vercel (프론트엔드)
1. https://vercel.com 가입 → New Project → 같은 GitHub 레포 선택
2. Root Directory를 `web`으로 지정 (Framework는 Vite 자동 인식)
3. 환경변수 `VITE_API_BASE`에 3번에서 나온 Render URL 입력 후 배포

### 5. 자동 갱신 확인
GitHub 레포 → Actions 탭 → "Refresh match data" 워크플로를 `Run workflow`로 한 번 수동 실행해서 정상 동작하는지 확인. 이후 매일 자동으로 돈다.
