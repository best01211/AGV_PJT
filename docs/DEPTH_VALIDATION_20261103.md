# 추가 3건의 고정 보정식 검증

기존 4건으로 만든 `config/depth_experimental_profile.json`을 변경하지 않고, 새 0.4/0.7/1.0m 기록만 평가했습니다. Base Metric Hypersim ViT-B, 입력 294, 동일 300×300 렌즈 보정과 중앙 20×20 ROI를 사용했습니다. 모델 선택과 보정 계수는 새 자료를 보기 전에 고정한 값입니다.

| 실측 | Base 원본 | 고정 보정식 결과 | 오차 |
|---|---|---|---|
| 0.400m | 1.503m | 0.346m | -5.4cm |
| 0.700m | 2.094m | 0.658m | -4.2cm |
| 1.000m | 2.635m | 0.943m | -5.7cm |

평균 절대 오차는 약 **5.1cm**, 최대 절대 오차는 약 **5.7cm**이며, 세 건 모두 실제보다 짧게 추정됐습니다. 이 수치를 새 보정식에 다시 맞춰 축소하지 않았습니다. 새 자료까지 계수 학습에 합치면 현재 검증 결과를 그대로 독립 검증 성과로 사용할 수 없기 때문입니다.

원본 RGB는 서버에 저장된 각 sample_id의 파일을 사용했습니다. 첨부로 지정된 Downloads의 `depth_unvalidated (1).npz`는 도구 확인 시 해당 경로에 없었지만, 서버의 원본/보정 영상/깊이 배열/실측 CSV에 세 기록이 모두 있어 그 정확한 대응 자료를 사용했습니다.

## 해석

- 같은 나무색 벽 장면의 중앙 거리에는 다점 보정이 유효할 가능성이 확인됐습니다.
- 여러 재질·조명·각도·카메라 자세, 화면 전체 픽셀에 대한 검증은 아닙니다.
- 5cm 수준 오차가 목표 집게/물체에 충분한지는 확인되지 않았습니다.
- 카메라 원본 보정 NPZ가 아닌 PDF의 반올림 K/D를 사용한 상태입니다.
- 실시간 웹은 아직 Small 원본 출력을 유지합니다. 실험 보정식은 Base 294 전용이며 Small 값에 곱하면 안 됩니다.
- `enabled=false`, `metric_validated=false`, `usable_for_control=false`, `apply_to_dense_depth=false`를 유지합니다.

## 재현

```powershell
.\.depth-venv\Scripts\python -m depth.benchmark --encoders vitb --input-sizes 294 --output data/depth_comparison/20261103_validation
.\.venv\Scripts\python -m depth.validate_distance --profile config/depth_experimental_profile.json --predictions data/depth_comparison/20261103_validation/predictions.csv --output data/depth_comparison/20261103_validation/heldout.json
```

두 번째 도구는 기존 학습 sample_id를 제외하고 같은 모델·입력 크기·장면의 새 관측만 평가하며, 범위 밖 값은 외삽하지 않습니다. 결과 JSON에는 원본 프로파일의 SHA256을 기록해 계수 변경 여부를 확인할 수 있습니다.
