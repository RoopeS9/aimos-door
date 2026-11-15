"""Anchored usual orders and human-controlled earned freedom."""
from __future__ import annotations

import copy
import uuid

from fastapi import HTTPException

from door.security import digest
from door.mandates import demo_scope_limits


class Autonomy:
    async def initialize_history(self):
        """Seed without making API startup depend on a live restaurant quote."""
        self.history_status = {"state": "seeding", "seeded": False, "customer_id": "restaurant"}
        try:
            with self.store.connect() as db:
                seed = self.store.get(db, "history_seed", "restaurant")
            from door.staples import RESTAURANT_SEED_VERSION
            needs_reseed = not seed or seed.get("seed_version") != RESTAURANT_SEED_VERSION
            if seed and not needs_reseed:
                lines = seed.get("lines", [])
                needs_reseed = not (
                    12 <= len(lines) <= 15
                    and len({line["id"] for line in lines}) == len(lines)
                    and len({line["category"] for line in lines}) >= 5
                    and sum(line["product_type"] == "drinking milk" for line in lines) == 1
                    and len(seed.get("history", [])) == 12
                )
            if needs_reseed:
                # Migrate the old three-line demo once. Thereafter reset restores
                # this exact immutable seed rather than requesting a fresh quote.
                from door.history import build_history
                seed = await build_history(self.catalog, self.valio)
                seed.setdefault("anchor_id", digest(seed["lines"]))
                with self.store.transaction() as db:
                    self.store.put(db, "history_seed", "restaurant", seed)
            with self.store.transaction() as db:
                if needs_reseed or not self.store.get(db, "usual", "restaurant"):
                    self._install_history(db, seed)
        except Exception as error:
            # Valio outages and an undeliverable staple are demo-history failures,
            # not process-liveness failures. The next restaurant read can retry.
            self.history_status = {
                "state": "unavailable", "seeded": False, "customer_id": "restaurant",
                "message": "Restaurant synthetic history is not seeded; hotel ordering remains available and a restaurant read will retry.",
                "detail": str(error)[:300],
            }
            return False
        self.history_status = {"state": "ready", "seeded": True, "customer_id": "restaurant", "message": "Restaurant synthetic history is ready."}
        return True

    async def ensure_history(self, customer_id="restaurant"):
        """Retry a missing restaurant seed on demand, never in a retry loop."""
        if customer_id != "restaurant" or getattr(self, "history_status", {}).get("state") == "ready":
            return True
        return await self.initialize_history()

    def _install_history(self, db, seed):
        usual = {**copy.deepcopy(seed), "customer_id": "restaurant", "anchor_id": seed.get("anchor_id") or digest(seed["lines"]), "anchor_confirmed": True}
        self.store.put(db, "usual", "restaurant", usual)
        customer = self.store.get(db, "customer", "restaurant")
        original = self.store.get(db, "mandate", customer["mandate_id"])["payload"]
        mandate = copy.deepcopy(original)
        mandate["jti"], mandate["version"] = "restaurant-history-v" + str(original["version"] + 1), original["version"] + 1
        mandate["limits"].update(per_order_cap=400, monthly_budget=6000, **demo_scope_limits(), must_hold_by_type={"drinking milk": ["lactose-free"]})
        token = self.keys.sign_buyer("restaurant", mandate)
        self.store.put(db, "mandate", mandate["jti"], {"mandate": token, "payload": mandate})
        customer.update(mandate_id=mandate["jti"], history_label="Synthetic order history: 12 weekly orders")
        self.store.put(db, "customer", "restaurant", customer)
        self.store.put(db, "freedom", "restaurant:usual basket", {"current_freedom": .1, "ask_next": False, "status": "Active", "reason": "The buyer's confirmed usual basket is the fixed anchor. The agent currently has 10% freedom per line.", "kind": "usual basket", "clean_approvals": []})
        for proposal in self.store.list(db, "proposal"):
            if proposal["customer_id"] == "restaurant" and proposal["kind"] == "usual basket":
                db.execute("DELETE FROM documents WHERE kind='proposal' AND id=?", (proposal["id"],))
        evidence = [{"order_id": entry.get("id", f"synthetic-{i}"), "date": entry.get("date") or entry.get("delivery_date"), "description": "A synthetic human approval without correction.", "synthetic": True} for i, entry in enumerate(usual["history"][-4:])]
        self.store.put(db, "proposal", "restaurant-departure-seed", {"id": "restaurant-departure-seed", "customer_id": "restaurant", "kind": "usual basket", "limit_id": "departure", "type": "freedom", "proposed_freedom": .2, "status": "pending", "evidence": evidence, "description": "Four clean synthetic approvals support increasing current freedom from 10% to 20%. The signed ceiling remains 30%."})

    def usual(self, customer_id):
        with self.store.connect() as db:
            usual = self.store.get(db, "usual", customer_id)
        return usual or {"customer_id": customer_id, "lines": [], "history": [], "synthetic": False, "anchor_confirmed": False, "message": "This buyer has no confirmed usual-order history."}

    def _departure(self, db, mandate, payload):
        customer = (mandate or {}).get("customer_id", "unknown")
        kind = payload.get("need", {}).get("product_type", "unknown")
        limits = (mandate or {}).get("limits", {})
        ceiling = float(limits.get("departure_ceiling", .3))
        state = self.store.get(db, "freedom", f"{customer}:{kind}") or {}
        freedom = min(ceiling, float(state.get("current_freedom", limits.get("current_freedom", .1))))
        usual = self.store.get(db, "usual", customer)
        if not usual:
            asking = state.get("ask_next", False)
            return {"passed": not asking, "notify": False, "within_ceiling": True, "maximum": 0, "reason": state.get("reason") if asking else "This buyer has no confirmed usual-order history, so the departure check is not applied."}
        baseline = {str(line["id"]): float(line["packs"]) for line in usual["lines"]}
        actual = {}
        for line in payload.get("lines", []):
            actual[str(line["id"])] = actual.get(str(line["id"]), 0) + float(line["packs"])
        departures = {pid: abs(actual.get(pid, 0) - baseline.get(pid, 0)) / baseline[pid] if baseline.get(pid, 0) else 1.0 for pid in baseline.keys() | actual.keys()}
        maximum = max(departures.values(), default=0)
        within = maximum <= ceiling + 1e-9
        stale = payload.get("need", {}).get("anchor_id") not in (None, usual["anchor_id"])
        passed = within and not state.get("ask_next") and not stale
        if stale:
            reason = "The buyer has confirmed a newer usual-order anchor. Review this order against that anchor before approval."
        elif state.get("ask_next"):
            reason = "A human correction tightened this kind of order. The agent must ask before ordering again. " + state.get("reason", "")
        else:
            reason = f"The largest line departure from the fixed human-confirmed usual order is {maximum:.1%}; current freedom is {freedom:.1%} and the signed ceiling is {ceiling:.1%}."
        return {"passed": passed, "notify": passed and maximum > freedom + 1e-9, "within_ceiling": within, "maximum": maximum, "reason": reason, "departures": departures}

    def _tighten(self, db, customer, kind, reason):
        state = self.store.get(db, "freedom", f"{customer}:{kind}") or {"kind": kind}
        state.update(current_freedom=0, ask_next=True, status="tightened", reason=reason, clean_approvals=[], pattern_approvals={})
        self.store.put(db, "freedom", f"{customer}:{kind}", state)
        for proposal in self.store.list(db, "proposal"):
            if proposal["customer_id"] == customer and proposal["kind"] == kind:
                db.execute("DELETE FROM documents WHERE kind='proposal' AND id=?", (proposal["id"],))

    def _clean_approval(self, db, order, mandate):
        if order.get("edited_by_human"):
            return
        if not all(check["passed"] for check in order["checks"] if check["id"] != 9) or not self._departure(db, mandate, order["offer_payload"])["within_ceiling"]:
            return
        customer, kind = order["customer_id"], order["offer_payload"]["need"]["product_type"]
        state = self.store.get(db, "freedom", f"{customer}:{kind}") or {"kind": kind, "current_freedom": mandate["limits"].get("current_freedom", .1), "ask_next": False, "status": "Active", "reason": "The buyer signed this current freedom.", "clean_approvals": []}
        approvals = state.setdefault("clean_approvals", [])
        if order["id"] not in approvals:
            approvals.append(order["id"])
        pattern = digest(sorted((str(line["id"]), float(line["packs"])) for line in order["offer_payload"]["lines"]))
        pattern_approvals = state.setdefault("pattern_approvals", {}).setdefault(pattern, [])
        if order["id"] not in pattern_approvals:
            pattern_approvals.append(order["id"])
        self.store.put(db, "freedom", f"{customer}:{kind}", state)
        proposals = self.store.list(db, "proposal")
        if len(approvals) >= 4 and not any(p["customer_id"] == customer and p["kind"] == kind for p in proposals):
            proposed = min(float(mandate["limits"]["departure_ceiling"]), float(state["current_freedom"]) + .1)
            if proposed > state["current_freedom"]:
                id = str(uuid.uuid4())
                self.store.put(db, "proposal", id, {"id": id, "customer_id": customer, "kind": kind, "limit_id": "departure", "type": "freedom", "proposed_freedom": proposed, "status": "pending", "evidence": [{"order_id": oid, "description": "Explicit human approval without an edit."} for oid in approvals[-4:]], "description": "Four clean approvals support this proposal. Current freedom has not changed."})
            elif len(pattern_approvals) >= 4:
                usual = self.store.get(db, "usual", customer)
                if usual and pattern != digest(sorted((str(line["id"]), float(line["packs"])) for line in usual["lines"])):
                    id = str(uuid.uuid4())
                    proposed_lines = copy.deepcopy(order["offer_payload"]["lines"])
                    self.store.put(db, "proposal", id, {"id": id, "customer_id": customer, "kind": kind, "limit_id": "departure", "type": "anchor", "status": "pending", "previous_anchor_id": usual["anchor_id"], "proposed_anchor_id": digest(proposed_lines), "proposed_lines": proposed_lines, "evidence": [{"order_id": oid, "description": "Explicit human approval of this exact order pattern."} for oid in pattern_approvals[-4:]], "description": "Four explicit human approvals of this exact basket support making it the usual order. The existing anchor has not moved."})

    def accept_proposal(self, proposal_id, body, buyer_token: str | None = None):
        with self.store.connect() as db:
            proposal = self.store.get(db, "proposal", proposal_id)
        if not proposal:
            raise HTTPException(404, "No such pending proposal exists.")
        session = self.buyer_session(buyer_token, proposal["customer_id"])
        if body.get("confirmed") is not True:
            raise HTTPException(409, "The buyer must confirm this proposal before freedom can increase.")
        with self.store.transaction() as db:
            proposal = self.store.get(db, "proposal", proposal_id)
            if not proposal:
                raise HTTPException(404, "No such pending proposal exists.")
            if body.get("customer_id") != proposal["customer_id"]:
                raise HTTPException(403, "This proposal belongs to a different buyer.")
            customer = self.store.get(db, "customer", proposal["customer_id"])
            mandate = self.keys.verify_buyer(self.store.get(db, "mandate", customer["mandate_id"])["mandate"], customer["bot_id"])
            key = f"{customer['id']}:{proposal['kind']}"
            state = self.store.get(db, "freedom", key) or {"kind": proposal["kind"]}
            if proposal["type"] == "anchor":
                usual = self.store.get(db, "usual", customer["id"])
                if usual["anchor_id"] != proposal["previous_anchor_id"]:
                    raise HTTPException(409, "The usual anchor changed after this proposal was prepared. Review a new proposal.")
                usual.update(lines=copy.deepcopy(proposal["proposed_lines"]), anchor_id=proposal["proposed_anchor_id"], anchor_confirmed=True, total=round(sum(line["total"] for line in proposal["proposed_lines"]), 2))
                usual["anchor"] = {"id": usual["anchor_id"], "lines": copy.deepcopy(usual["lines"]), "confirmed": True, "confirmed_by": "simulated buyer accepting the anchor proposal", "synthetic": True}
                self.store.put(db, "usual", customer["id"], usual)
                state.update(current_freedom=min(float(state.get("current_freedom", 0)), float(mandate["limits"]["departure_ceiling"])), status="Anchor accepted by buyer", reason="The buyer accepted an exact new usual-order anchor after four explicit approvals. Signed ceilings and current freedom did not increase.")
            else:
                state.update(current_freedom=min(float(proposal["proposed_freedom"]), float(mandate["limits"]["departure_ceiling"])), ask_next=False, status="Accepted by buyer", reason="The buyer accepted the loosening proposal after reviewing four clean approvals.")
            self.store.put(db, "freedom", key, state)
            acceptance = {**proposal, "accepted": True, "order_kind": proposal["kind"], "kind": "autonomy-acceptance", "jti": str(uuid.uuid4()), "mandate_id": mandate["jti"], "exp": mandate["exp"], "resulting_freedom": state.get("current_freedom")}
            acceptance["buyer_signature"] = self.keys.sign_buyer(customer["id"], acceptance)
            self.store.put(db, "accepted_proposal", proposal_id, acceptance)
            state.update(clean_approvals=[], pattern_approvals={})
            self.store.put(db, "freedom", key, state)
            db.execute("DELETE FROM documents WHERE kind='proposal' AND id=?", (proposal_id,))
            record = self._decision_record(
                db, customer_id=customer["id"], action="proposal_accepted", subject_id=proposal_id,
                description=f"The buyer accepted {proposal['type']} proposal {proposal_id}; current freedom is {state.get('current_freedom')} and the signed ceiling is {mandate['limits']['departure_ceiling']}.",
                approved_by=f"simulated buyer session {session['id']}",
                details={"proposal_id": proposal_id, "proposal_type": proposal["type"], "resulting_freedom": state.get("current_freedom"), "accepted_document_id": acceptance["jti"]},
            )
        return {"status": "accepted", "record_id": record["id"], "record_hash": record["hash"], **self.limits(customer["id"])}
