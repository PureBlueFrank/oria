# Scenario B remediation ablation

> Diagnostic use only: sb-v1-043 was previously exposed. This does not replace the frozen V0.4-T05 failed card.

- Status: `in_progress` (2/20)
- Conservative recorded cost: `$0.029859` / `$8.00`
- Pricing snapshot: `deepseek-20260907`

| Group | Runs | Clean pass | Safe fail-closed | Mean calls | Median latency (s) | Cost (USD) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `g00_control` | 0 | 0 | 0 | - | - | 0.000000 |
| `g01_budget` | 1 | 0 | 0 | 0.0 | 10.025077377999878 | 0.000000 |
| `g10_model` | 1 | 0 | 0 | 4.0 | 312.65228356400075 | 0.029859 |
| `g11_both` | 0 | 0 | 0 | - | - | 0.000000 |

## Failure types

- `g00_control`: `{}`
- `g01_budget`: `{"provider_or_infrastructure": 1}`
- `g10_model`: `{"provider_or_infrastructure": 1}`
- `g11_both`: `{}`
