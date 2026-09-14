# lcwo - CW practice grading
#
#   make            start recording a session (same as `make session`)
#   make report     build the HTML report and open it
#   make user       who is being recorded for
#   make help       everything else

PYTHON ?= python3
LCWO   := $(PYTHON) lcwo.py

.DEFAULT_GOAL := session
.PHONY: session record report html user groups trouble practice key speed delete \
        restore trash purge db merge test clean help

# every data target takes U=<call sign> to work as another operator
OP = $(if $(U),-u $(U))
ALL = $(if $(EVERYONE),--everyone)

## session: record a session (interactive)  [U=<call sign>]
session:
	@$(LCWO) record $(OP)

record: session

## report: build the HTML report and open it in a browser  [U= EVERYONE=1]
report:
	@$(LCWO) report --open $(OP) $(ALL)

## html: build the HTML report without opening it
html:
	@$(LCWO) report $(OP) $(ALL)

## user: list operators, or switch/add one  [USE=<call> | ADD=1 NAME= CALL=]
user:
	@$(LCWO) user $(if $(USE),--use $(USE)) $(if $(ADD),--add) \
		$(if $(NAME),--name "$(NAME)") $(if $(CALL),--call $(CALL)) \
		$(if $(REMOVE),--remove $(REMOVE))

## groups: list every group with its accuracy and trouble letters
groups:
	@$(LCWO) groups $(OP) $(ALL)

## trouble: trouble letters  [D=<last N days>] [N=<threshold>] [G=<group>]
trouble:
	@$(LCWO) trouble $(if $(D),-d $(D)) $(if $(N),-n $(N)) $(if $(G),-g $(G)) $(OP) $(ALL)

## practice: sending practice from trouble letters  [D= N= C= CHARS= PAIRS=1]
practice:
	@$(LCWO) practice $(if $(D),-d $(D)) $(if $(N),-n $(N)) $(if $(C),-c $(C)) \
		$(if $(CHARS),--chars $(CHARS)) $(if $(G),-g $(G)) \
		$(if $(SEED),--seed $(SEED)) $(if $(PLAIN),--plain) \
		$(if $(PAIRS),--pairs) $(OP) $(ALL)

## key: attach a results table to a session left ungraded
key:
	@$(LCWO) key $(if $(S),-s $(S)) $(OP)

## speed: show speeds, or set one  [G=<group> CHAR=<wpm> EFF=<wpm>]
speed:
	@$(LCWO) speed $(if $(G),-g $(G)) $(if $(CHAR),--char $(CHAR)) $(if $(EFF),--eff $(EFF)) $(OP)

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
	@echo "  tables: operators, settings, groups, sessions, runs"
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
	@echo "  variables: U= operator  G= group  S= session  R= run"
	@echo "             D= last N practice days   N= trouble threshold"
	@echo "             C= how many groups   CHARS= practise these instead"
	@echo "             CHAR=/EFF= wpm   Y=1 skip prompt   APPLY=1 write merge"
	@echo "             EVERYONE=1 every operator at once"
