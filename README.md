# VOLT Discord Bot

VOLT 내전 운영을 위한 Discord 봇입니다. 내전 생성, 공지 버튼 참여, 신청 취소, 명단 확인, 드래프트, 결과 기록, 랭킹 조회를 지원합니다.

## Features

- `!내전생성 <제목>` 실행 시 자동으로 모집 공지 생성
- 공지 메시지에서 `참여하기`, `신청취소`, `명단 보기`, `운영 메뉴` 버튼 사용 가능
- 정원 10명 도달 시 자동으로 모집 마감 처리
- `!1` 입력 시 메인 패널에서 주요 기능을 버튼으로 제어 가능
- 운영 패널에서 내전 생성, 관리, 드래프트, 결과 기록, 삭제 가능
- 삭제된 내전 번호는 다음 생성 시 자동으로 재사용
- 서버 관리자는 일반 멤버에게 봇 운영권한을 부여/회수 가능
- `BOT_CHANNEL_ID`를 설정하면 텍스트 명령은 봇 제어 채널에서만 동작
- `ANNOUNCEMENT_CHANNEL_ID`, `RESULT_CHANNEL_ID`를 설정하면 모집/결과 채널을 분리 가능
- `!내랭킹`, `!전체랭킹`, `!점수표`로 랭킹과 점수 확인 가능

## Files

- `bot.py`: 메인 봇 코드
- `requirements.txt`: Python 패키지 목록
- `.env.example`: 필요한 환경변수 예시
- `.gitignore`: 로컬 비밀값과 캐시 제외 설정

## Environment Variables

아래 값들은 GitHub가 아니라 Render 환경변수에 넣어야 합니다.

- `DISCORD_TOKEN`: 디스코드 봇 토큰
- `DATABASE_URL`: PostgreSQL 연결 문자열
- `DB_SSLMODE`: Supabase 사용 시 보통 `require`
- `PORT`: 기본값 `8080`
- `LOG_LEVEL`: 기본값 `INFO`
- `ALLOW_DUPLICATE_SIGNUPS`: 테스트 중에는 `true`, 정식 출시 때는 `false`
- `BOT_CHANNEL_ID`: 텍스트 명령과 `!1` 패널을 사용할 봇 제어 채널 ID
- `ANNOUNCEMENT_CHANNEL_ID`: 내전 모집 공지를 올릴 채널 ID
- `RESULT_CHANNEL_ID`: 결과 요약을 올릴 채널 ID

## Local Run

```bash
pip install -r requirements.txt
python bot.py
```

## Render Deploy

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
python bot.py
```

권장 환경변수:

```text
DISCORD_TOKEN=...
DATABASE_URL=...
DB_SSLMODE=require
PORT=8080
LOG_LEVEL=INFO
ALLOW_DUPLICATE_SIGNUPS=true
BOT_CHANNEL_ID=123456789012345678
ANNOUNCEMENT_CHANNEL_ID=123456789012345679
RESULT_CHANNEL_ID=123456789012345680
```

정식 출시 전에는 아래처럼 변경하세요.

```text
ALLOW_DUPLICATE_SIGNUPS=false
```

## Commands

유저 명령어:

- `!1`
- `!신청`
- `!신청취소 <방번호>`
- `!내전목록`
- `!명단 <방번호>`
- `!내랭킹`
- `!전체랭킹`
- `!점수표`

운영진 명령어:

- `!내전생성 <제목>`
- `!내전삭제 <방번호>`
- `!내전종료 <방번호>`
- `!명단수정 <방번호>`
- `!드래프트 <방번호> <캡틴1> <캡틴2>`
- `!결과기록 <방번호> <승리팀번호>`
- `!운영권한부여 <멘션 또는 유저ID>`
- `!운영권한회수 <멘션 또는 유저ID>`
- `!운영권한목록`

## Notes

- 테스트 모드에서는 같은 계정 중복 신청을 허용할 수 있습니다.
- 정식 출시 전 반드시 `ALLOW_DUPLICATE_SIGNUPS=false`로 변경하세요.
- 실제 비밀값이 들어간 `.env` 파일은 GitHub에 올리면 안 됩니다.
