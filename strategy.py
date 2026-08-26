import math
from dataclasses import dataclass


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, sigma, kind, r=0.04, q=0.012):
    if T <= 0.0 or sigma <= 0.0:
        return max(S - K, 0.0) if kind == "call" else max(K - S, 0.0)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    if kind == "call":
        return S * math.exp(-q * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp(-q * T) * norm_cdf(-d1)


def bs_delta(S, K, T, sigma, kind, r=0.04, q=0.012):
    if T <= 0.0 or sigma <= 0.0:
        if kind == "call":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / sq
    nd1 = norm_cdf(d1)
    if kind == "call":
        return math.exp(-q * T) * nd1
    return math.exp(-q * T) * (nd1 - 1.0)


def strike_for_delta(S, T, sigma, kind, target, r=0.04, q=0.012):
    lo, hi = S * 0.3, S * 2.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        d = bs_delta(S, mid, T, sigma, kind, r, q)
        if kind == "put":
            if d > -target:
                lo = mid
            else:
                hi = mid
        else:
            if d < target:
                hi = mid
            else:
                lo = mid
    return 0.5 * (lo + hi)


@dataclass
class Config:
    structure: str = "strangle"
    delta: float = 0.16
    stop_mult: float = 2.0
    tp_mult: float = 0.5
    iv_scale: float = 1.0
    r: float = 0.04
    q: float = 0.012
    slippage: float = 0.03
    commission: float = 0.65
    account: float = 25000.0
    contracts: int = 1
    vix_max: float = 0.0
    vix_min: float = 0.0
    skip_bwd: bool = False
    trend_filter: bool = False
    clock: str = "trading"
    credit_haircut: float = 1.0
    exit_iv_mult: float = 1.0
    fail_closed_vix9d: bool = True

    def __post_init__(self):
        if self.structure not in ("strangle", "put", "call"):
            raise ValueError(f"structure must be strangle/put/call, got {self.structure!r}")
        if not 0.01 <= self.delta <= 0.49:
            raise ValueError(f"delta must be in [0.01, 0.49], got {self.delta}")
        if self.stop_mult <= 0:
            raise ValueError(f"stop_mult must be >0, got {self.stop_mult}")
        if self.tp_mult < 0:
            raise ValueError(f"tp_mult must be >=0, got {self.tp_mult}")
        if self.iv_scale <= 0:
            raise ValueError(f"iv_scale must be >0, got {self.iv_scale}")
        if self.credit_haircut <= 0 or self.credit_haircut > 1.5:
            raise ValueError(f"credit_haircut must be in (0, 1.5], got {self.credit_haircut}")
        if self.exit_iv_mult <= 0 or self.exit_iv_mult > 5:
            raise ValueError(f"exit_iv_mult must be in (0, 5], got {self.exit_iv_mult}")
        if self.slippage < 0 or self.commission < 0:
            raise ValueError("slippage/commission must be >=0")
        if self.account <= 0 or self.contracts < 1:
            raise ValueError("account must be >0 and contracts >=1")
        if self.clock not in ("trading", "calendar"):
            raise ValueError(f"clock must be trading/calendar, got {self.clock!r}")
        if self.vix_max < 0 or self.vix_min < 0:
            raise ValueError("vix_max/vix_min must be >=0")
        # 0 means disabled; otherwise require vix_min <= vix_max when both set
        if self.vix_max and self.vix_min and self.vix_min > self.vix_max:
            raise ValueError(f"vix_min {self.vix_min} > vix_max {self.vix_max}")
