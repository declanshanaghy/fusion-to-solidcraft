# Tool-side targets. Day-to-day runs (convert, verify) start from your designs
# workspace, which calls into this repo; see README.
PY := python3

.PHONY: install-fusion check

install-fusion:   ## link the ExportAll script into Fusion's Scripts folder
	fusion/install.sh

check:            ## syntax-check every script
	$(PY) -m py_compile common.py convert/convert.py verify/check.py fusion/ExportAll/ExportAll.py
	@find . -name __pycache__ -prune -exec rm -rf {} +
	@echo ok
