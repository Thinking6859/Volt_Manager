# VOLT Discord Bot

VOLT 내전 운영을 위한 Discord 봇입니다. 내전 생성, 공지 버튼 참여, 신청취소, 명단 확인, 드래프트, 결과 기록, 랭킹 조회를 지원합니다.

## Features

- `!내전생성 <제목>` 실행 시 자동 모집 공지 생성
- 공지 메시지에서 `참여하기`, `신청취소`, `명단 보기` 버튼 사용 가능
- 정원 10명 도달 시 자동 모집 마감 처리
- `!드래프트`, `!결과기록`으로 경기 운영 가능
- `!내랭킹`, `!전체랭킹`으로 랭킹 조회 가능

## Files

- `bot.py`: 메인 봇 코드
- `requirements.txt`: Python 패키지 목록
- `.env.example`: 필요한 환경변수 예시

## Environment Variables

아래 값들은 GitHub가 아니라 Render 환경변수에 넣어야 합니다.

- `DISCORD_TOKEN`: 디스코드 봇 토큰
- `DATABASE_URL`: PostgreSQL 연결 문자열
- `PORT`: 기본값 `8080`
- `LOG_LEVEL`: 기본값 `INFO`
- `ALLOW_DUPLICATE_SIGNUPS`: 테스트 중에는 `true`, 정식 출시 때는 `false`

## Local Run

```bash
pip install -r requirements.txt
python bot.py
```

## Render Deploy

Start Command 예시:

```bash
pip install -r requirements.txt && python bot.py
```

환경변수:

- `DISCORD_TOKEN`
- `DATABASE_URL`
- `PORT=8080`
- `LOG_LEVEL=INFO`
- `ALLOW_DUPLICATE_SIGNUPS=true`

정식 출시 전에는 아래처럼 변경하세요.

```text
ALLOW_DUPLICATE_SIGNUPS=false
```

## Commands

유저 명령어:

- `!신청`
- `!신청취소 <방번호>`
- `!내전목록`
- `!명단 <방번호>`
- `!내랭킹`
- `!전체랭킹`
- `!점수표`

운영진 명령어:

- `!내전생성 <제목>`
- `!내전종료 <방번호>`
- `!드래프트 <방번호> <캡틴1> <캡틴2>`
- `!결과기록 <방번호> <승리팀번호>`

## Notes

- 테스트 모드에서는 같은 계정 중복 신청이 허용됩니다.
- 정식 출시 시 반드시 `ALLOW_DUPLICATE_SIGNUPS=false`로 바꿔야 합니다.
- 실제 비밀값이 들어간 `.env` 파일은 GitHub에 올리면 안 됩니다.
