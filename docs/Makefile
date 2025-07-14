# Minimal makefile for Sphinx documentation
#

# You can set these variables from the command line, and also
# from the environment for the first two.
SPHINXOPTS    ?=
# Since this project uses Poetry, it's best to run sphinx-build in its environment.
SPHINXBUILD   ?= poetry run sphinx-build
SOURCEDIR     = .
BUILDDIR      = _build

# Put it first so that "make" without argument is like "make help".
help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

.PHONY: help serve html clean

html:
	@$(SPHINXBUILD) -b html "$(SOURCEDIR)" "$(BUILDDIR)/html" $(SPHINXOPTS)

# Target to build and host the documentation locally.
serve:
	@$(MAKE) html
	@echo "Serving docs at http://localhost:8000/ (Ctrl-C to stop)"
	@cd "$(BUILDDIR)/html" && python3 -m http.server 8000

clean:
	@rm -rf "$(BUILDDIR)"
