# lcwo - CW practice grading
#
#   make            start recording a session (same as `make session`)
#   make report     build the HTML report and open it
#   make help       everything else

PYTHON ?= python3
LCWO   := $(PYTHON) lcwo.py

.DEFAULT_GOAL := session
.PHONY: session record report html groups trouble key speed delete restore \
        trash purge db merge test clean help

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

## delete: move a group/session/run to the bin  [G= or S= or R=, Y=1 skips prompt]
delete:
	@$(LCWO) delete $(if $(G),-g $(G)) $(if $(S),-s $(S)) $(if $(R),-r $(R)) $(if $(Y),-y)

## restore: bring one back from the bin  [G= or S= or R=]
restore:
	@$(LCWO) restore $(if $(G),-g $(G)) $(if $(S),-s $(S)) $(if $(R),-r $(R))

## trash: list what is in the bin
trash:
	@$(LCWO) trash

## purge: permanently delete everything in the bin (asks first)
purge:
	@$(LCWO) purge

## db: open a SQLite shell on the database
db:
	@command -v sqlite3 >/dev/null || { echo "sqlite3 not installed"; exit 1; }
	@echo "  tables: groups, sessions, runs"
	@echo "  views : live_groups, live_sessions, live_runs  (exclude binned rows)"
	@echo "  .quit to exit"
	@sqlite3 -header -column $(or $(LCWO_DB),lcwo.db)

## merge: fold groups sharing an assignment into one  [APPLY=1 to write]
merge:
	@$(LCWO) merge $(if $(APPLY),--apply)

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
		| sed -e 's/^## //' -e 's/: */\t/' \
		| awk -F'\t' '{printf "  %-13s %s\n", $$1, $$2}'
	@echo
	@echo "  variables: G= group  S= session  R= run  N= threshold"
	@echo "             CHAR=/EFF= wpm   Y=1 skip prompt   APPLY=1 write merge"
