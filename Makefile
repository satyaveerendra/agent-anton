# Thin wrappers (SPEC §3, §19). PY points at the project venv by default.
PY ?= .venv/bin/python
UNIT = -m pytest -q -m "not integration and not acceptance"

.PHONY: test verify-0 verify-surface verify-target target discover discover-subaccount replay replay-notfound \
        verify-1 verify-1b verify-2 verify-3 verify-4 verify-5a verify-5b verify-6 verify-7 \
        verify-evidence verify-all approve
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

# --- available from later phases (SPEC §17); each fails loudly until its phase lands ---
define not_yet
	@echo "$(1): not built until $(2) (SPEC §17)"; exit 1
endef
verify-5a:       ; $(call not_yet,verify-5a,phase 6)
verify-5b:       ; $(call not_yet,verify-5b,phase 6)
verify-6:        ; $(call not_yet,verify-6,phase 7)
verify-evidence: ; $(call not_yet,verify-evidence,phase 8)
verify-all:      ; $(call not_yet,verify-all,phase 8)
