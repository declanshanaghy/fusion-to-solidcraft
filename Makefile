# Tool-side targets. Day-to-day runs (convert, verify) start from your designs
# workspace, which calls into this repo; see README.
PY := python3

.PHONY: install-fusion check hooks

install-fusion:   ## link the Fusion scripts into Fusion's Scripts folder
	fusion/install.sh

check:            ## syntax-check every script and run the tests
	find . -name '*.py' -not -path './.git/*' -print0 | xargs -0 $(PY) -m py_compile
	$(PY) -m unittest discover -s tests -q
	@find . -name __pycache__ -prune -exec rm -rf {} +
	@echo ok

hooks:            ## run `make check` before every push from this clone
	git config core.hooksPath .githooks
	@echo "pre-push hook enabled"
