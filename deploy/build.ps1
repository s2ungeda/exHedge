# TrdBot 배포판 빌드 스크립트 (파이썬 설치 불필요한 .exe 생성)
#   실행:  .\deploy\build.ps1     (프로젝트 루트에서)
#
# 산출물: deploy\dist\TrdBot\  (이 폴더를 통째로 배포)
#   TrdBot.exe  본체(창 모드, 콘솔 숨김)
#   setup.exe   API 키·계좌비번 등록(콘솔)
#   .env        계좌번호 등 설정 템플릿
#   사용법.txt
#
# 갱신 방식(중요): 본체(onedir)는 **스테이징 폴더**에 빌드한 뒤 _internal·TrdBot.exe 만
#   배포 폴더로 교체한다. .env·config 같은 사용자 편집 파일은 절대 덮지 않는다.
#   (예전엔 onedir 의 COLLECT 가 배포 폴더를 통째로 지워 .env 가 날아갔음 — 그 재발 방지)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = ".venv\Scripts\pyinstaller.exe"

# 빌드 스탬프 — 캡션에 표시할 빌드 날짜/시각을 소스에 심어 exe(_internal)에 함께 번들.
# (exe+_internal 만 다른 PC로 옮겨도 빌드 날짜가 따라간다)
$buildStamp = Get-Date -Format "yyyy-MM-dd HH:mm"
Set-Content "src\trdbot\_buildstamp.py" "BUILD = `"$buildStamp`"" -Encoding utf8
Write-Host "빌드 스탬프: $buildStamp"

# 공통 옵션 — keyring 백엔드/pydantic 은 PyInstaller 자동탐지가 놓치므로 명시 수집.
# (distpath 는 빌드마다 다르게 주므로 여기서 제외)
$common = @(
  "--paths", "src",
  "--collect-all", "keyring",
  "--collect-all", "pydantic",
  "--collect-all", "pydantic_settings",
  "--hidden-import", "keyring.backends.Windows",
  "--hidden-import", "trdbot._buildstamp",   # 빌드 스탬프 모듈(캡션 빌드 날짜) 번들 포함
  "--workpath", "deploy\build",
  "--specpath", "deploy",
  "--noconfirm"
)

$dist  = "deploy\dist\TrdBot"
$stage = "deploy\dist_stage"

Write-Host "[1/4] 설정 exe(setup.exe) 빌드..."
& $py --name setup --console --onefile @common --distpath "deploy\dist" "examples\00_setup_credentials.py"

Write-Host "[2/4] 본체 exe(TrdBot.exe) 빌드 → 스테이징..."
# onedir 의 COLLECT 는 distpath 폴더를 통째로 지운다 → 실사용 폴더가 아닌 스테이징에 빌드.
& $py --name TrdBot --windowed --onedir @common --distpath $stage "src\trdbot\__main__.py"

Write-Host "[3/4] 본체 바이너리만 교체(런타임 파일 보존)..."
New-Item -ItemType Directory -Force $dist | Out-Null
# _internal 통째 교체 + exe 교체. .env / tokens / logs / config 는 건드리지 않는다.
if (Test-Path "$dist\_internal") { Remove-Item "$dist\_internal" -Recurse -Force }
Copy-Item "$stage\TrdBot\_internal" "$dist\_internal" -Recurse -Force
Copy-Item "$stage\TrdBot\TrdBot.exe" "$dist\TrdBot.exe" -Force
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "[4/4] 배포 폴더 조립..."
# 비밀값·로그 제거 — 남에게 넘길 때 내 토큰/로그가 섞이면 안 됨(access 토큰은 실행 시 keyring 으로 재발급).
Remove-Item "$dist\tokens", "$dist\logs" -Recurse -Force -ErrorAction SilentlyContinue
# setup.exe 이동(배포 폴더 안에만 유지).
Copy-Item "deploy\dist\setup.exe" "$dist\setup.exe" -Force
Remove-Item "deploy\dist\setup.exe" -Force -ErrorAction SilentlyContinue
# 사용법은 항상 최신으로 갱신.
Copy-Item "deploy\files\사용법.txt" "$dist\사용법.txt" -Force
# .env 는 사용자가 계좌번호를 채워둘 수 있으므로 이미 있으면 덮지 않는다(템플릿은 최초 1회만).
if (-not (Test-Path "$dist\.env")) { Copy-Item "deploy\files\.env" "$dist\.env" }
# config — 휴일 목록·체결음. 사용자 갱신분이 있으면 유지, 없으면 기본 복사.
New-Item -ItemType Directory -Force "$dist\config" | Out-Null
if (-not (Test-Path "$dist\config\holidays.txt")) { Copy-Item "config\holidays.txt" "$dist\config\holidays.txt" -Force }
if (-not (Test-Path "$dist\config\fill.wav")) { Copy-Item "config\fill.wav" "$dist\config\fill.wav" -Force }

Write-Host "완료 → $dist  (이 폴더를 배포하세요)"
Write-Host "주의: 남에게 배포할 땐 .env 의 계좌번호를 비우고 tokens\ 폴더가 없는지 확인하세요."
