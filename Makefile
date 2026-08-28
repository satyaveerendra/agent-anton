# Thin wrappers (SPEC §3, §19). PY points at the project venv by default.
PY ?= .venv/bin/python
UNIT = -m pytest -q -m "not integration and not acceptance"

.PHONY: test verify-0 verify-surface verify-target target discover replay replay-notfound \
        verify-1 verify-1b verify-2 verify-3 verify-4 verify-5a verify-5b verify-6 verify-7 \
        verify-evidence verify-all

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

# --- available from later phases (SPEC §17); each fails loudly until its phase lands ---
define not_yet
	@echo "$(1): not built until $(2) (SPEC §17)"; exit 1
endef
discover:        ; $(call not_yet,discover,phase 5)
replay:          ; $(call not_yet,replay,phase 4)
replay-notfound: ; $(call not_yet,replay-notfound,phase 4)
verify-1:        ; $(call not_yet,verify-1,phase 4)
verify-1b:       ; $(call not_yet,verify-1b,phase 4)
verify-2:        ; $(call not_yet,verify-2,phase 4)
verify-3:        ; $(call not_yet,verify-3,phase 4)
verify-4:        ; $(call not_yet,verify-4,phase 4)
verify-7:        ; $(call not_yet,verify-7,phase 4)
verify-5a:       ; $(call not_yet,verify-5a,phase 6)
verify-5b:       ; $(call not_yet,verify-5b,phase 6)
verify-6:        ; $(call not_yet,verify-6,phase 7)
verify-evidence: ; $(call not_yet,verify-evidence,phase 8)
verify-all:      ; $(call not_yet,verify-all,phase 8)
