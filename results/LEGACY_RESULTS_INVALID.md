# Invalid Legacy Results

The root-level files `opt_is.csv`, `opt_oos.csv`, `trades_2026.csv`,
`months_2026.csv`, and `equity_curve_2026.png` were produced before the engine
integrity corrections. They are retained only to preserve the audit trail.

Do not use their performance figures. The old optimizer selected its final
winner using 2026 results, the put-delta calculation was wrong, and the option
prices were synthetic Black-Scholes estimates rather than historical quotes.

Only immutable directories created by the corrected scripts should be cited,
and their manifests must still be read with the synthetic-price warning.
