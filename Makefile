# lcwo - CW practice grading
#
#   make            start recording a session (same as `make session`)
#   make report     build the HTML report and open it
#   make help       everything else

PYTHON ?= python3
LCWO   := $(PYTHON) lcwo.py

.DEFAULT_GOAL := session
.PHONY: session record report html groups trouble key speed test clean help

## session: record a session (interactive)
session:
	@$(LCWO)

record: session

## report: build the HTML report and open it in a browser
report:
	@$(LCWO) report --open

## html: build the HTML report without opening it
html:
	@$(LCWO) report

## groups: list every group with its accuracy and trouble letters
groups:
	@$(LCWO) groups

## trouble: trouble letters in the terminal  [N=<threshold>] [G=<group>]
trouble:
	@$(LCWO) trouble $(if $(N),-n $(N)) $(if $(G),-g $(G))

## key: attach a results table to a session left ungraded
key:
	@$(LCWO) key $(if $(S),-s $(S))

## speed: show speeds, or set one  [G=<group> CHAR=<wpm> EFF=<wpm>]
speed:
	@$(LCWO) speed $(if $(G),-g $(G)) $(if $(CHAR),--char $(CHAR)) $(if $(EFF),--eff $(EFF))

## test: run the Python checks and the browser-side report tests
test:
	@$(LCWO) selftest
	@$(PYTHON) test_report.py

## clean: remove Python bytecode caches (leaves the database and reports alone)
clean:
	@rm -rf __pycache__
	@echo "  cleaned __pycache__ (lcwo.db and reports/ untouched)"

## help: list the targets
help:
	@echo "lcwo - CW practice grading"
	@echo
	@grep -E '^## ' $(MAKEFILE_LIST) \
		| sed -e 's/^## //' -e 's/: */|/' \
		| awk -F'|' '{printf "  %-13s %s\n", $$1, $$2}'
	@echo
	@echo "  variables: N= threshold  G= group  S= session  CHAR=/EFF= wpm"
