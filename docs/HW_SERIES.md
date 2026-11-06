# Hardware series

A short sampler for the host that runs llama-server. It does not call the model, and it does not change server flags or restart anything.

## Run

```sh
python3 scripts/hw_series_sample.py --once
python3 scripts/hw_series_sample.py --install
```

`--install` is for macOS. It writes a user LaunchAgent with `StartInterval` 180 and starts it at login. The label defaults to `com.llm-appliance.hw-series`.

The log is `~/claude-stack/data/runtime/hw_series.jsonl`. Each row's `ts` is America/Chicago with a numeric offset. The hour of `ts` (0-23) is the hour index a decode series can line up with.

The file keeps the last 2160 rows. At one row every 180 seconds that is 108 hours, which is more than 36 hours. Older rows are dropped. A row that contains a home path, an address, or a ticket id is not written.

## What a row holds

Swap used and total come from `vm.swapusage`. Wired and free memory come from `vm_stat`. `memory_pressure -Q` supplies the free percentage. `memory_pressure` on the row is the word from `kern.memorystatus_vm_pressure_level`: normal, warn, urgent, or critical. That word is written even when it says normal and swap is high. The raw `vm.memory_pressure` sysctl is stored under its own name. It is not a percentage, and a 0 does not mean the machine is idle.

`pid` and `lstart` are the llama-server process. A new `lstart` is a restart. Set `HW_SERIES_PROC` if the process name is more specific than `llama-server`.

Temperatures (`cpu_c`, `gpu_c`, `ane_c`) stay null when the host has no cheap sensor. The sampler tries one `powermetrics` sample with `sudo -n`. If that needs a password or returns no temperatures, it records that once and does not ask again.

`/metrics` is read from `127.0.0.1` port 18080 with a two second timeout. A cache-size gauge is stored as `kv_cache_tokens`, or `kv_cache_ratio` when the gauge is a ratio. Cumulative counters are not a size and are not written. `prompt_tokens_cached_total` counts prompt tokens reused so far. `n_tokens_max` is the largest sequence length seen since the process started. If there is no cache-size gauge, the field is omitted.

## Hourly summary

`summarize_day(rows, day)` returns `hourly_temp`, `hourly_swap`, `hourly_pressure`, and `hourly_kv`. `day` is `YYYY-MM-DD` in America/Chicago. An empty list means that sensor was missing. The summary does not say why a decode rate moved.
