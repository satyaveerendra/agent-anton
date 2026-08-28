# Thin wrappers (SPEC §3, §19). PY points at the project venv by default.
PY ?= .venv/bin/python
UNIT = -m pytest -q -m "not integration and not acceptance"

.PHONY: test verify-0 verify-surface verify-target target discover discover-subaccount replay replay-notfound \
        verify-1 verify-1b verify-2 verify-3 verify-4 verify-5a verify-5b verify-6 verify-7 \
        verify-evidence verify-all approve demo-discovery-approval demo-discovery-approval-auto replay-beta curate-evidence
ACC = -m pytest -q -m acceptance tests/acceptance/test_aw_replay.py

test:
	$(PY) $(UNIT)

verify-0:            ## phases 1 and 3: ruff + the unit suite
	$(PY) -m ruff check .
	$(PY) $(UNIT)

verify-surface:      ## phase 3: the Playwright integration suite
	$(PY) -m pytest -q -m integration

comma := ,
target:              ## serve the Member Servicing Console: make target TENANT=beta INJECT=expire,modal PORT=8000
	$(PY) -m anton target serve --tenant $${TENANT:-alpha} $(foreach i,$(subst $(comma), ,$(INJECT)),--inject $(i)) --port $${PORT:-8000}

verify-target:       ## phase 2: curl every pinned page and injection against a freshly spawned app
	$(PY) -m pytest -q -m acceptance tests/acceptance/test_verify_target.py

# --- phase 5: discovery ------------------------------------------------------------------
discover:            ## discover the balance capability against a running app (needs ANTHROPIC_API_KEY in env or .env)
	$(PY) -m anton discover --goal "Look up member $${MEMBER:-204817} and read the current savings balance" \
	  --profile profiles/servicing.yaml --spec specs/read_savings_balance.yaml --param member_id=$${MEMBER:-204817} --out $${OUT:-artifacts}

discover-subaccount: ## discover the risky sub-account capability (caller consent for the confirm click)
	$(PY) -m anton discover --goal "Open a new savings sub-account with a \$$50 initial deposit for member 204817 and reach the confirmation screen" \
	  --profile profiles/servicing.yaml --spec specs/open_subaccount.yaml --param member_id=204817 --param initial_deposit=50 \
	  --param nickname="Holiday fund" --approve-risky --out $${OUT:-artifacts}

# --- phase 4: replay ---------------------------------------------------------------------
replay:              ## replay the balance capability against a running app: make replay MEMBER=204817
	$(PY) -m anton replay artifacts/read_savings_balance.json --param member_id=$${MEMBER:-204817}

replay-notfound:     ## the MEMBER_NOT_FOUND business outcome (exit 10)
	$(PY) -m anton replay artifacts/read_savings_balance.json --param member_id=999999

approve:             ## (re)approve both hand-authored artifacts
	$(PY) -m anton approve artifacts/read_savings_balance.json --by $${USER}
	$(PY) -m anton approve artifacts/open_subaccount.json --by $${USER}

verify-1:            ## AW-1 happy-path replay
	$(PY) $(ACC) -k "test_aw1_"
verify-1b:           ## AW-1b a capability, not a macro
	$(PY) $(ACC) -k "test_aw1b_"
verify-2:            ## AW-2 business outcome, not a crash
	$(PY) $(ACC) -k "test_aw2_"
verify-3:            ## AW-3 recoverable and transient conditions
	$(PY) $(ACC) -k "test_aw3"
verify-4:            ## AW-4 hard failure is debuggable; redaction holds under failure
	$(PY) $(ACC) -k "test_aw4_"
verify-7:            ## AW-7 risky step gating
	$(PY) $(ACC) -k "test_aw7"

# --- phase 6: escalation -------------------------------------------------------------------
verify-5a:           ## AW-5a escalation parks, is observable through the console, can be abandoned / times out / is denied
	$(PY) -m pytest -q -m acceptance tests/acceptance/test_aw5a.py

verify-5b:           ## AW-5b HUMAN round-trip (definition of done): take control, click Confirm in the live window, hand back
	$(PY) tests/acceptance/verify_5b.py

demo-discovery-approval: ## phase-6 human demo: discover with --escalate, operator clicks Approve once
	$(PY) tests/acceptance/demo_discovery_approval.py

demo-discovery-approval-auto: ## the same path with a simulated operator (mechanics only; not the human gate)
	$(PY) tests/acceptance/demo_discovery_approval.py --auto

# --- phase 7: tenant overlay -----------------------------------------------------------------
# SPEC §19: a line matching '^- P7-CUT ·' in docs/decisions.md (or ANTON_PHASE7_CUT=1) marks the phase cut;
# prose mentions never count. verify-all honours the same rule.
PHASE7_CUT := $(shell (grep -q '^- P7-CUT ·' docs/decisions.md || [ "$(ANTON_PHASE7_CUT)" = "1" ]) && echo 1)
verify-6:            ## AW-6 cross-tenant overlay (beta): fails without overlays/beta.yaml, passes with it
ifeq ($(PHASE7_CUT),1)
	@echo "verify-6: CUT (phase 7 marked '- P7-CUT ·' in docs/decisions.md or ANTON_PHASE7_CUT=1); skipped"
else
	$(PY) -m pytest -q -m acceptance tests/acceptance/test_aw6.py
endif

replay-beta:         ## replay the alpha-recorded balance artifact on tenant beta through the overlay
	$(PY) -m anton replay artifacts/read_savings_balance.json --param member_id=$${MEMBER:-204817} --overlay overlays/beta.yaml

# --- phase 8: evidence + the whole gate -----------------------------------------------------
verify-evidence:     ## every seeded SSN/DOB, the demo password and the key prefix are absent from evidence/, artifacts/, runs/
	$(PY) -m anton.evidence.verify

curate-evidence:     ## copy the newest run matching each AW into evidence/<folder>/ and refresh INDEX.md (run after the AW targets)
	$(PY) -m anton.evidence.curate

verify-all:          ## SPEC §19: every non-cut target; then reminders for the human gates
	$(MAKE) verify-0
	$(MAKE) verify-surface
	$(MAKE) verify-1
	$(MAKE) verify-1b
	$(MAKE) verify-2
	$(MAKE) verify-3
	$(MAKE) verify-4
	$(MAKE) verify-5a
	$(MAKE) verify-6
	$(MAKE) verify-7
	$(MAKE) verify-evidence
	@echo ""
	@echo "verify-all: every non-cut automated target is green."
	@echo "REMINDER  AW-5b (human, definition of done): make verify-5b — take control, click Confirm in the live window, hand back."
	@echo "REMINDER  discovery gate (human, SPEC §19): check evidence/discovery-run* + evidence/artifact/* per docs/progress.md, and make demo-discovery-approval."
