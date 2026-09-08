# Scenario B remediation ablation

> Diagnostic use only: sb-v1-043 was previously exposed. This does not replace the frozen V0.4-T05 failed card.

- Status: `complete` (20/20)
- Conservative recorded cost: `$0.391077` / `$8.00`
- Pricing snapshot: `deepseek-20260907`

| Group | Runs | Clean pass | Safe fail-closed | Mean calls | Median latency (s) | Cost (USD) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `g00_control` | 5 | 0 | 0 | 4.0 | 31.2275181670002 | 0.048469 |
| `g01_budget` | 5 | 0 | 0 | 7.0 | 79.46555155799979 | 0.091816 |
| `g10_model` | 5 | 0 | 1 | 4.0 | 200.3911446940001 | 0.097696 |
| `g11_both` | 5 | 0 | 1 | 7.0 | 240.5711673610003 | 0.153096 |

## Failure types

- `g00_control`: `{"structured_output_error": 5}`
- `g01_budget`: `{"provider_or_infrastructure": 5}`
- `g10_model`: `{"budget_wall_time": 1, "evidence_validation_failed": 1}`
- `g11_both`: `{"budget_wall_time": 1, "evidence_validation_failed": 1, "provider_or_infrastructure": 2, "structured_output_error": 1}`
