# Assessment | Ship a Multi-Tool Agent

## Scenario: Trip Concierge

**Tools chosen:** `search_flights`, `search_hotels`, `calculate`

I chose the trip-concierge scenario because it requires genuine multi-step
reasoning: the agent must gather two independent data sources (flights, hotels),
perform arithmetic to combine them, and then make a budget judgement — none of
which can be answered in a single tool call. The three tools have clear,
non-overlapping responsibilities, which makes the agent's decision-making
visible in the trace.

### Tool responsibilities

| Tool | Job |
|---|---|
| `search_flights(origin, destination)` | Returns available flights on a route with prices in EUR |
| `search_hotels(city, nights)` | Returns hotel options with per-night price and total stay cost |
| `calculate(expression)` | Evaluates arithmetic to sum costs and check budget |

---

## Reliability

**Step limit:** The agent loop is capped at `MAX_STEPS = 8`. After 8 iterations
without a final answer the loop aborts and returns
`{"error": "Agent could not complete goal within 8 steps."}`.
This prevents an infinite loop if the model keeps requesting tools without
converging, which can happen with free-tier rate-limit retries or ambiguous goals.

**Graceful tool-error handling:** Every tool call is wrapped in a `try/except`
inside the loop. If a tool raises an unexpected exception the loop catches it,
wraps it in `{"error": "..."}`, appends it as a tool result, and lets the model
decide how to proceed rather than crashing the whole agent.
If a tool explicitly returns `{"error": "..."}` (e.g. unknown route), the model
sees the error message and can either try a different argument or tell the user
it couldn't fulfil the request.

---

## Safety

**Mitigation: argument validation before every tool call**

Every tool validates its arguments against an allowlist or range check *before*
any data lookup or `eval` is performed:

- `search_flights` — IATA codes are checked with `re.fullmatch(r"[A-Z]{3}", code)`.
  A SQL-injection-style string like `"LH'; DROP TABLE flights;--"` is caught and
  rejected immediately.
- `search_hotels` — city must be in a hard-coded `KNOWN_CITIES` set; `nights`
  must be an integer between 1 and 30. Arbitrary strings or out-of-range numbers
  are rejected.
- `calculate` — the expression is checked against an allowlist regex
  (`[0-9+\-*/.()\ %a-zA-Z_]+`) and a separate blocklist for dangerous tokens
  (`__`, `import`, `exec`, `eval`, `open`, `os`, `sys`).
  An injection attempt like `__import__('os').system('rm -rf /')` is blocked
  before `eval` ever runs.

**What attack does this defend against?**
A prompt-injection inside a flight description or hotel review could try to
convince the model to call `calculate("__import__('os').listdir('/')")` or
`search_hotels("../../../etc/passwd", 1)`. Validating every argument at the
tool boundary means the model's text output cannot cause code execution or
path traversal — the validation layer is independent of the model and cannot be
overridden by anything in the conversation.

---

## Captured Run

```
GOAL: I'm flying from London (LHR). Plan a 3-night trip to Porto under €600
      total. Give me the best flight and a hotel that fits the budget, and
      confirm whether the total is within €600.
------------------------------------------------------------

[step 1] Calling model ...
  TOOL CALL  -> search_flights({'origin': 'LHR', 'destination': 'OPO'})
  TOOL RESULT<- {"route": "LHR->OPO", "options": [{"airline": "TAP Air Portugal", ...},
                 {"airline": "Ryanair", "flight": "FR8821", "price_eur": 62, ...}, ...]}

[step 2] Calling model ...
  TOOL CALL  -> search_hotels({'city': 'porto', 'nights': 3})
  TOOL RESULT<- {"city": "porto", "nights": 3, "options": [
                 {"name": "Maison Particuliere", "price_per_night_eur": 95, "total_eur": 285, ...},
                 {"name": "Gallery Hostel", "price_per_night_eur": 45, "total_eur": 135, ...}, ...]}

[step 3] Calling model ...
  TOOL CALL  -> calculate({'expression': '62 + 285'})
  TOOL RESULT<- {"expression": "62 + 285", "result": 347}

[step 4] Calling model ...

[step 4] Model returned final answer.

============================================================
STRUCTURED OUTPUT
============================================================
{
  "destination": "porto",
  "duration_nights": 3,
  "budget_eur": 600,
  "flight": {
    "airline": "Ryanair",
    "flight_number": "FR8821",
    "origin": "LHR",
    "destination": "OPO",
    "price_eur": 62
  },
  "hotel": {
    "name": "Maison Particuliere",
    "stars": 4,
    "area": "Ribeira",
    "price_per_night_eur": 95,
    "total_eur": 285
  },
  "cost_breakdown": {
    "flight_eur": 62,
    "hotel_eur": 285,
    "total_eur": 347
  },
  "within_budget": true,
  "notes": "3-night Porto trip via Ryanair LHR->OPO staying at a 4-star Ribeira hotel totals €347, well within the €600 budget."
}

============================================================
SAFETY DEMO - argument injection attempt
============================================================
Calling calculate with a malicious expression:
  Result: {'error': "Expression contains potentially dangerous token: \"__import__('os').system('rm -rf /')\""} 

Calling search_flights with an invalid IATA code:
  Result: {'error': "'origin' must be a 3-letter IATA code (got \"LH'; DROP TABLE flights;--\")."} 

Calling search_hotels with an unknown city:
  Result: {'error': "Unknown city 'atlantis'. Supported cities: ['lisbon', 'london', 'madrid', 'paris', 'porto']."} 
```

---

## How to Run

```bash
# 1. Clone and enter the repo
git clone https://github.com/Jabrail-Atakishiyev/m9-08-assessment
cd m9-08-assessment

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your Gemini API key (never commit this)
export GOOGLE_API_KEY="your-free-gemini-key"

# 4. Run the agent
python agent.py
```

No API key is committed to the repository.
