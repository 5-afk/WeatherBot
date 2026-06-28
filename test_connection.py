from dotenv import load_dotenv
load_dotenv()
from src.kalshi_client import KalshiClient
from src.risk_manager import RiskManager
from src.position_sizer import PositionSizer

print("=== CONNECTION TEST ===")
k = KalshiClient()
print(f"Environment:      {k.env.upper()}")
print(f"Base URL:         {k.base_url}")
print(f"Has credentials:  {k.has_credentials}")
print(f"Connection test:  {k.test_connection()}")

print()
print("=== BUDGET MODEL ===")
r = RiskManager()
print(f"Starting balance: ${r.starting_balance:.2f}")
print(f"Daily budget:     ${r.daily_budget:.2f}")
print(f"Max bet:          ${r.max_bet_usd:.2f}")
print(f"Max open pos:     {r.max_open_positions}")
print(f"Daily loss limit: ${r.daily_loss_limit:.2f}")
print(f"Todays budget:    ${r.get_todays_budget():.2f}")
pocketed = float(r._get_state("total_pocketed") or 0)
running = float(r._get_state("running_budget") or r.starting_balance)
print(f"Total pocketed:   ${pocketed:.2f}")
print(f"Running budget:   ${running:.2f}")

print()
print("=== POSITION SIZER TEST ===")
p = PositionSizer()
size = p.size_trade(
    win_probability=0.85,
    price=0.45,
    confidence=0.93,
    current_budget=100.0,
    previous_payout=0.0,
    last_trade_won=False
)
print(f"Bet at $0.45 price | 85% win prob | 93% confidence")
print(f"  Stake:         ${size.stake:.2f}")
print(f"  Contracts:     {size.contracts}")
print(f"  Kelly size:    ${size.kelly_size:.2f}")
print(f"  Reason:        {size.reason}")
profit = size.contracts * (1 - 0.45)
print(f"  Profit if win: ${profit:.2f}")

print()
print("=== MARKET FETCH TEST ===")
markets = k.list_markets("KXHIGHNY")
if markets:
    m = markets[0]
    print(f"First NYC market:  {m.ticker}")
    print(f"YES ask:           ${m.yes_ask:.4f}" if m.yes_ask else "YES ask: None")
    print(f"NO ask:            ${m.no_ask:.4f}" if m.no_ask else "NO ask: None")
    print(f"Settlement:        {m.settlement_time}")
    raw_keys = [k for k in m.raw.keys() if "price" in k.lower() or "ask" in k.lower() or "bid" in k.lower()]
    print(f"Price fields:      {raw_keys}")
else:
    print("No markets returned")