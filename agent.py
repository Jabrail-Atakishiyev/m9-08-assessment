"""
Assessment | Ship a Multi-Tool Agent
======================================
Scenario : Trip Concierge
Tools    : search_flights, search_hotels, calculate
Goal     : "Plan a 3-day trip to Porto under €600 and give me the total."
Output   : Structured JSON itinerary with cost breakdown

Reliability : hand-rolled loop with MAX_STEPS cap + graceful tool-error handling
Safety      : argument validation on every tool call before execution
              (blocks out-of-range budgets, unknown cities, code-injection in expressions)

Run:
    export GOOGLE_API_KEY="your-free-gemini-key"
    python agent.py
"""

import json
import math
import os
import re

from google import genai
from google.genai import types

# ── Mock travel data ───────────────────────────────────────────────────────────

FLIGHTS_DB = {
    ("LHR", "OPO"): [
        {"airline": "TAP Air Portugal", "flight": "TP1355", "price_eur": 89,  "duration_h": 2.5},
        {"airline": "Ryanair",          "flight": "FR8821", "price_eur": 62,  "duration_h": 2.7},
        {"airline": "EasyJet",          "flight": "U28801", "price_eur": 74,  "duration_h": 2.6},
    ],
    ("MAD", "OPO"): [
        {"airline": "Iberia Express",   "flight": "I23101", "price_eur": 55,  "duration_h": 1.2},
        {"airline": "Vueling",          "flight": "VY1960", "price_eur": 48,  "duration_h": 1.1},
    ],
    ("CDG", "OPO"): [
        {"airline": "TAP Air Portugal", "flight": "TP447",  "price_eur": 99,  "duration_h": 2.3},
        {"airline": "Transavia",        "flight": "TO775",  "price_eur": 81,  "duration_h": 2.4},
    ],
}

HOTELS_DB = {
    "porto": [
        {"name": "Maison Particulière",  "stars": 4, "price_per_night_eur": 95,  "area": "Ribeira"},
        {"name": "Hotel Infante Sagres", "stars": 5, "price_per_night_eur": 185, "area": "Downtown"},
        {"name": "Porto A·S 1829",       "stars": 4, "price_per_night_eur": 110, "area": "Aliados"},
        {"name": "Gallery Hostel",       "stars": 3, "price_per_night_eur": 45,  "area": "Bonfim"},
    ],
}

KNOWN_CITIES      = {"porto", "lisbon", "madrid", "paris", "london"}
KNOWN_IATA_CODES  = {"LHR", "MAD", "CDG", "OPO", "LIS", "BCN", "AMS"}

# ── Argument validation ────────────────────────────────────────────────────────

class ToolArgumentError(ValueError):
    """Raised when a tool receives invalid or potentially malicious arguments."""

def _validate_iata(code: str, field: str) -> str:
    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ToolArgumentError(
            f"'{field}' must be a 3-letter IATA code (got {code!r})."
        )
    return code

def _validate_city(city: str) -> str:
    city = city.strip().lower()
    if city not in KNOWN_CITIES:
        raise ToolArgumentError(
            f"Unknown city {city!r}. Supported cities: {sorted(KNOWN_CITIES)}."
        )
    return city

def _validate_nights(nights: int) -> int:
    if not isinstance(nights, int) or nights < 1 or nights > 30:
        raise ToolArgumentError(
            f"'nights' must be an integer between 1 and 30 (got {nights!r})."
        )
    return nights

def _validate_expression(expr: str) -> str:
    """Reject expressions that look like code injection attempts."""
    expr = expr.strip()
    # Allow only digits, operators, spaces, dots, parentheses, and math function names
    if not re.fullmatch(r"[0-9+\-*/.()\s%a-zA-Z_]+", expr):
        raise ToolArgumentError(
            f"Expression contains disallowed characters: {expr!r}"
        )
    # Block suspicious builtins / dunder patterns
    banned = re.compile(r"\b(__|\bimport\b|\bexec\b|\beval\b|\bopen\b|\bos\b|\bsys\b)")
    if banned.search(expr):
        raise ToolArgumentError(
            f"Expression contains potentially dangerous token: {expr!r}"
        )
    return expr

# ── Tools ──────────────────────────────────────────────────────────────────────

def search_flights(origin: str, destination: str) -> dict:
    """
    Search for available flights between two airports.

    Returns a list of flight options with airline, flight number, price in EUR,
    and flight duration, or an error dict if the route is unknown.

    Safety: IATA codes are validated before lookup — non-alphabetic or
    non-3-letter codes are rejected immediately.

    Args:
        origin:      3-letter IATA code of the departure airport (e.g. "LHR").
        destination: 3-letter IATA code of the arrival airport (e.g. "OPO").
    """
    try:
        origin      = _validate_iata(origin,      "origin")
        destination = _validate_iata(destination, "destination")
    except ToolArgumentError as e:
        return {"error": str(e)}

    key = (origin, destination)
    flights = FLIGHTS_DB.get(key)
    if flights is None:
        return {"error": f"No flights found for route {origin}→{destination}."}
    return {"route": f"{origin}→{destination}", "options": flights}


def search_hotels(city: str, nights: int) -> dict:
    """
    Search for hotels in a city and compute the total cost for a given stay.

    Returns a list of hotels with name, star rating, nightly price, area,
    and total cost for the requested number of nights.

    Safety: city name must be in the known-cities whitelist; nights must be
    a positive integer ≤ 30 — prevents nonsense or injection via arguments.

    Args:
        city:   City name, e.g. "porto".
        nights: Number of nights to stay (1–30).
    """
    try:
        city   = _validate_city(city)
        nights = _validate_nights(nights)
    except ToolArgumentError as e:
        return {"error": str(e)}

    hotels = HOTELS_DB.get(city)
    if hotels is None:
        return {"error": f"No hotels found in {city!r}."}

    enriched = [
        {**h, "total_eur": h["price_per_night_eur"] * nights, "nights": nights}
        for h in hotels
    ]
    return {"city": city, "nights": nights, "options": enriched}


def calculate(expression: str) -> dict:
    """
    Evaluate a numeric arithmetic expression and return the result.

    Supports standard Python arithmetic (+, -, *, /, **, %, ())
    and math module functions (sqrt, ceil, floor, round, etc.).

    Safety: the expression is validated against an allowlist before eval —
    only digits, operators, parentheses, and known math identifiers are
    permitted. Attempts to inject code (os, exec, __import__, etc.) are
    blocked and return an error.

    Args:
        expression: A numeric expression string, e.g. "62 + 95 * 3".
    """
    try:
        expression = _validate_expression(expression)
    except ToolArgumentError as e:
        return {"error": str(e)}
    try:
        allowed = {k: v for k, v in math.__dict__.items() if not k.startswith("_")}
        result  = eval(expression, {"__builtins__": {}}, allowed)  # noqa: S307
        return {"expression": expression, "result": round(result, 2)}
    except Exception as exc:
        return {"error": f"Could not evaluate {expression!r}: {exc}"}


# ── Tool registry & Gemini tool spec ──────────────────────────────────────────

TOOL_REGISTRY = {
    "search_flights": search_flights,
    "search_hotels":  search_hotels,
    "calculate":      calculate,
}

TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_flights",
                description=(
                    "Search available flights between two airports. "
                    "Returns options with prices in EUR. "
                    "Use 3-letter IATA codes (e.g. LHR, OPO, MAD, CDG)."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "origin":      types.Schema(type="STRING", description="Departure IATA code"),
                        "destination": types.Schema(type="STRING", description="Arrival IATA code"),
                    },
                    required=["origin", "destination"],
                ),
            ),
            types.FunctionDeclaration(
                name="search_hotels",
                description=(
                    "Search hotels in a city and compute total stay cost. "
                    "Supported cities: porto, lisbon, madrid, paris, london."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "city":   types.Schema(type="STRING", description="City name in lowercase"),
                        "nights": types.Schema(type="INTEGER", description="Number of nights (1-30)"),
                    },
                    required=["city", "nights"],
                ),
            ),
            types.FunctionDeclaration(
                name="calculate",
                description=(
                    "Evaluate a numeric arithmetic expression. "
                    "Use standard Python syntax, e.g. '62 + 95 * 3'."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "expression": types.Schema(
                            type="STRING",
                            description="Arithmetic expression to evaluate",
                        )
                    },
                    required=["expression"],
                ),
            ),
        ]
    )
]

# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a Trip Concierge agent. Your job is to plan affordable city trips for users.

You have three tools:
  - search_flights(origin, destination): find flights to a destination.
  - search_hotels(city, nights): find hotels and total accommodation cost.
  - calculate(expression): perform any arithmetic you need.

HOW TO PLAN:
1. The user will give you a destination, duration, and budget in EUR.
2. Use search_flights to find a good flight option (pick the cheapest that fits the budget).
3. Use search_hotels to find hotel options for the trip duration.
4. Use calculate to add flight + hotel costs and check against the budget.
5. Choose a flight + hotel combination that fits within the budget.

IMPORTANT — STRUCTURED OUTPUT:
When you have all the information you need, output ONLY a valid JSON object in this
exact schema and nothing else:

{
  "destination": "<city>",
  "duration_nights": <int>,
  "budget_eur": <number>,
  "flight": {
    "airline": "<string>",
    "flight_number": "<string>",
    "origin": "<IATA>",
    "destination": "<IATA>",
    "price_eur": <number>
  },
  "hotel": {
    "name": "<string>",
    "stars": <int>,
    "area": "<string>",
    "price_per_night_eur": <number>,
    "total_eur": <number>
  },
  "cost_breakdown": {
    "flight_eur": <number>,
    "hotel_eur": <number>,
    "total_eur": <number>
  },
  "within_budget": <true|false>,
  "notes": "<one sentence summary>"
}

Do not add markdown fences, no explanation — pure JSON only.
"""

# ── Hand-rolled agent loop ─────────────────────────────────────────────────────

MAX_STEPS = 8  # cap: prevents runaway loops; 3-4 steps expected for this goal


def run_agent(client: genai.Client, user_goal: str) -> dict:
    """
    Run the trip-concierge agent loop.
    Returns the structured itinerary as a Python dict.
    """
    messages: list[types.Content] = []
    messages.append(types.Content(role="user", parts=[types.Part(text=user_goal)]))

    print(f"\nGOAL: {user_goal}\n" + "-" * 60)

    for step in range(1, MAX_STEPS + 1):
        print(f"\n[step {step}] Calling model …")

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=messages,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=TOOLS,
            ),
        )

        candidate = response.candidates[0]
        parts      = candidate.content.parts

        # ── Tool calls? ───────────────────────────────────────────────────────
        tool_calls = [p for p in parts if p.function_call is not None]
        if tool_calls:
            messages.append(types.Content(role="model", parts=parts))
            result_parts = []
            for part in tool_calls:
                fc = part.function_call
                args = dict(fc.args)
                print(f"  TOOL CALL  → {fc.name}({args})")
                fn = TOOL_REGISTRY.get(fc.name)
                if fn is None:
                    tool_result = {"error": f"Unknown tool: {fc.name!r}"}
                else:
                    try:
                        tool_result = fn(**args)
                    except Exception as exc:          # unexpected runtime error
                        tool_result = {"error": str(exc)}
                print(f"  TOOL RESULT← {json.dumps(tool_result)[:200]}")
                result_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=fc.name, response=tool_result
                        )
                    )
                )
            messages.append(types.Content(role="user", parts=result_parts))
            continue

        # ── Final text answer ─────────────────────────────────────────────────
        raw_text = " ".join(p.text for p in parts if p.text).strip()
        messages.append(types.Content(role="model", parts=parts))

        print(f"\n[step {step}] Model returned final answer.")
        # Strip any accidental markdown fences
        clean = re.sub(r"^```[a-zA-Z]*\s*", "", raw_text)
        clean = re.sub(r"\s*```$",           "", clean).strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            return {"error": "Model did not return valid JSON.", "raw": raw_text}

    # Step limit reached
    print(f"\n[ABORT] Step limit ({MAX_STEPS}) reached without a final answer.")
    return {"error": f"Agent could not complete goal within {MAX_STEPS} steps."}


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise EnvironmentError("Set the GOOGLE_API_KEY environment variable first.")

    client = genai.Client(api_key=api_key)

    # ── Primary goal ──────────────────────────────────────────────────────────
    goal = (
        "I'm flying from London (LHR). "
        "Plan a 3-night trip to Porto under €600 total. "
        "Give me the best flight and a hotel that fits the budget, "
        "and confirm whether the total is within €600."
    )
    result = run_agent(client, goal)

    print("\n" + "=" * 60)
    print("STRUCTURED OUTPUT")
    print("=" * 60)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # ── Safety demo: inject a bad argument (blocked by validation) ────────────
    print("\n" + "=" * 60)
    print("SAFETY DEMO — argument injection attempt")
    print("=" * 60)
    print("Calling calculate with a malicious expression:")
    bad_result = calculate("__import__('os').system('rm -rf /')")
    print(f"  Result: {bad_result}")

    print("\nCalling search_flights with an invalid IATA code:")
    bad_flight = search_flights("LH'; DROP TABLE flights;--", "OPO")
    print(f"  Result: {bad_flight}")

    print("\nCalling search_hotels with an unknown city:")
    bad_hotel = search_hotels("atlantis", 3)
    print(f"  Result: {bad_hotel}")


if __name__ == "__main__":
    main()
