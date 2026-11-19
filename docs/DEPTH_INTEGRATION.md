# 보정 PDF 기반 실시간 Depth 통합

현재 변경 사항: 실시간 모델은 Base 294로 전환됐고 나무색 벽 7점의 중앙 보정값을 별도로 표시합니다. 아래 문서는 최초 Small 통합 기록을 포함합니다. 현재 적용 범위·계수·기록 형식은 `DEPTH_RUNTIME_CORRECTION.md`가 기준입니다. 집기 정확도는 여전히 검증되지 않았습니다.

## 확인한 문제와 이번 적용

사용자 제공 `CALIBRATION_NOTION.pdf`를 확인했습니다. 렌즈 보정과 절대거리 검증은 별도 문제입니다. PDF의 0.334px RMS는 재투영 오차이며 거리 정확도 보증이 아닙니다. 임시 배율 0.349421 적용 후에도 0.8m 판이 1.2m 벽보다 멀게 추정됐으므로 동일한 전역 배율로 오차가 해결됐다고 볼 수 없습니다.

이번에는 다음을 구현하고 실제 JETANK 영상으로 실행했습니다.

1. PDF의 K/D를 `config/camera_calibration.json`에 전사했습니다. 원본 NPZ는 프로젝트 및 확인한 작업 폴더에서 찾지 못했습니다. 소수점이 반올림된 PDF 수치이므로 원본 보정 파일과 완전히 같다고 주장하지 않습니다.
2. `getOptimalNewCameraMatrix(alpha=0)`와 remap으로 300×300 영상의 왜곡을 보정합니다. ROI를 추가로 잘라내지 않고 전체 300×300 출력 좌표계를 유지합니다. 다른 해상도는 거부하며 강제 리사이즈하지 않습니다. 같은 해상도여도 다른 카메라·화각·크롭 설정에는 재사용하면 안 됩니다.
3. 공식 **metric_depth 구현**과 **Hypersim indoor ViT-S** 체크포인트를 사용합니다. 상대 깊이 모델로 대체하지 않았습니다. 모델 출력 상한 20m, 입력 크기 294입니다.
4. 최신 카메라 프레임을 소비하고 최소 세 소스 프레임 간격을 둡니다. 처리 중 쌓인 프레임은 큐에 누적하지 않습니다.
5. 보정 영상과 깊이 시각화, 중앙 20×20 영역 유효값 중앙값을 표시합니다. 중앙 400픽셀 중 유효값 200개 미만이면 숫자를 표시하지 않습니다. 0 이하·20m 이상·NaN/Inf와 보정 경계 무효 픽셀은 제외합니다.
6. 임시 한 점 배율은 적용하지 않습니다. 모델 원본 출력에 m 단위를 표시하되 정확한 거리로 검증되지 않았음을 함께 표시합니다. 지도·안전 정지·모터 제어에는 전달하지 않습니다.

## 실행

이미 구성한 환경에서는:

```powershell
.\scripts\start-video.ps1
```

http://127.0.0.1:8000/video 를 새로 열고 아래의 **Depth 시작**을 누릅니다. 카메라 수신도 함께 시작하며, 종료 시 GPU worker만 닫고 카메라 수신은 유지합니다. 카메라 연결 종료 시 Depth 출력도 최신 프레임이 없으면 숨깁니다. 서버 종료 시 worker를 정리합니다. 서버는 단일 worker로 실행합니다.

별도 `.depth-venv`가 기존 데스크톱 GPU PyTorch와 torchvision을 system-site-packages로 재사용합니다. OpenCV는 해당 별도 환경에 설치했습니다. 웹 서버의 `.venv`에는 무거운 AI 패키지를 추가하지 않았습니다. 추론은 별도 프로세스에서 수행하므로 GPU 모델 로딩/실행이 FastAPI 이벤트 루프를 차단하지 않습니다.

다른 Python 실행 경로는 서버 실행 전 `JETANK_DEPTH_PYTHON`으로 지정합니다. 모델 경로와 체크포인트는 `.depth-model/`, 보정 파일은 `config/camera_calibration.json`입니다. 모델 출처·커밋·해시는 `config/depth_model_manifest.json`에 기록했습니다. `.depth-venv`와 `.depth-model`은 Git에서 제외됩니다.

환경을 새로 구성하는 경우 GPU 환경에 맞는 torch/torchvision이 먼저 필요합니다. 기존 GPU Python을 재사용한다면 다음과 같이 별도 환경을 구성합니다.

```powershell
python -m venv --system-site-packages .depth-venv
.\.depth-venv\Scripts\python -m pip install opencv-python-headless
git clone https://github.com/DepthAnything/Depth-Anything-V2.git .depth-model/Depth-Anything-V2
git -C .depth-model/Depth-Anything-V2 checkout a561b849ebae10a6f5ef49e26c83cbbcd36c71bf
```

공식 체크포인트 출처는 manifest에 있으며 `.depth-model/checkpoints/depth_anything_v2_metric_hypersim_vits.pth`로 저장합니다. 이번 환경에서는 약 99MB 체크포인트를 내려받고 GPU 로딩을 확인했습니다. 기존 GPU 설치가 없는 환경에서는 `requirements-depth.txt`와 해당 장비의 torch/torchvision 호환성을 확인합니다. 서버가 모델을 자동으로 다운로드하거나 원격 코드를 실행하지는 않습니다.

## 화면과 데이터 해석

- 보정 영상과 Depth는 같은 처리 프레임에서 생성합니다. 원본 RGB 라이브 화면은 더 최신일 수 있습니다.
- 색상은 프레임별 자동 정규화 없이 근거리 확인용 0~3m 고정 범위입니다. 3m 이상의 유효 추정은 동일한 먼 거리 색상으로 표시하며 원본 수치의 20m 모델 범위를 바꾸지 않습니다. 가까울수록 빨강, 멀수록 파랑이며 범위 밖이나 무효 픽셀은 회색입니다. 가까운 평면만 보이면 색상 차이가 작을 수 있습니다.
- 흰 사각형은 중앙 20×20 ROI입니다. 숫자는 그 영역의 유효 추정값 중앙값입니다. 물체 식별이나 해당 표적의 거리임을 자동 보증하지 않습니다.
- 추정값에 대한 시간 필드는 PC의 영상 수신 시각입니다. 실제 센서 촬영 시각과 같다고 가정하지 않습니다.
- `inference_ms`는 디코딩·보정·추론·시각화·배열 저장까지 포함한 worker 처리 시간입니다. 전체 종단 지연 또는 실제 FPS가 아닙니다.
- 이번 실제 실행에서 약 32~63ms의 처리 시간을 확인했습니다. 측정 시점과 GPU 부하에 따라 달라집니다.
- 5초 이상 오래된 소스 결과는 표시/다운로드하지 않습니다. worker 오류나 해상도 불일치 시 실패 이유를 표시합니다.

## 실측 비교 기록

화면 중앙에 판이나 표적을 두고 거리와 장면명을 입력한 뒤 **현재 결과 기록**을 누릅니다. 입력한 거리는 현재 모델값에 맞춰 쓰지 말고 별도 측정값을 사용합니다. 광학 중심에서의 거리와 다른 기준점(로봇 앞면 등)을 혼동하지 않습니다. 시선 각도, 장면, 재질과 보정 설정을 실험 노트에 함께 기록합니다.

`data/depth_evaluation/`에 다음을 저장합니다.

- `measurements.csv`: 표적/실측/모델값/원본 프레임 번호/run ID
- 샘플별 원본 RGB JPEG, 왜곡 보정 JPEG, 깊이 NPZ
- NPZ: 모델 원본 깊이, 유효 마스크, 보정 후 내부 행렬, 소스 번호와 PC 수신 시각, 보정 출처, 거리 검증 미완료 표시

최근 결과를 프레임 번호와 추론 세션 ID별로 최대 15초·32프레임·64MB 범위에서 보관합니다. 화면 영상 요청과 기록 요청은 같은 번호의 자료를 사용하므로 새 추론이 진행돼도 표시했던 원본·보정 영상·깊이 배열·숫자가 함께 저장됩니다. 화면 업데이트가 멈춰 보관 기한이 지나거나 Depth가 재시작된 경우에만 409로 거부합니다. 원본 자료는 로컬에 저장되며 외부로 업로드하지 않습니다.

```powershell
.\.venv\Scripts\python -m depth.evaluate --input data/depth_evaluation/measurements.csv
```

MAE, bias, RMSE, 최대 오차와 거리 순서 역전을 계산합니다. PDF에 실린 4개 그룹 평균도 `config/depth_reference_evaluation.json`에 별도 기록했습니다. 여기서 평균의 절대 오차 평균은 약 0.32m, 순서 역전은 1쌍입니다. 이는 원본 8개 관측의 RMSE/MAE를 재계산한 것이 아니며, 과거 임시 배율 결과와 새 모델 원본 출력을 혼합해 평가하지 않습니다.

## 다음에 해결할 정확도 문제

렌즈 보정만으로 장면별 metric 오차를 제거할 수 없습니다. 여러 재질·거리에서 새 실측을 수집해 홀드아웃 조건으로 검증해야 합니다. 이 자료 없이 임의의 배율이나 오프셋을 만들어 정확도가 개선됐다고 보고하지 않습니다. PDF는 기존 저장 화면을 518 입력으로 다시 추론해도 순서 역전이 남았다고 기록하므로 입력 크기 변경도 해결책으로 단정하지 않습니다.

현재 8장의 프레임별 거리 하나와 PDF 그룹 평균만으로 픽셀별 미세조정을 수행하지 않습니다. 근거리 정지와 센서 지도에는 초음파 등 검증된 거리 입력을 우선 연결하고, Depth는 보조 정보로 둡니다. 새 데이터에서 오차가 목표를 충족하는지 먼저 평가해야 합니다.

## 검증

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -q
.\.depth-venv\Scripts\python -m unittest discover -s depth/tests -v
```

웹/기존 기능/Depth API 43개, 보정·후처리 4개가 통과했습니다. 별도로 실제 JETANK 프레임의 GPU 추론과 웹 보정·Depth 표시를 확인했습니다. 현장 거리 정확도를 입증한 테스트는 아닙니다.

참고: https://github.com/DepthAnything/Depth-Anything-V2/tree/main/metric_depth
