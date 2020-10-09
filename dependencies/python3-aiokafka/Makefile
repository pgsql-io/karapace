FEDORA = $(shell source /etc/os-release && echo $$VERSION_ID)
PROJ = aiokafka
PYNAME = aiokafka
GITHUB_ACCOUNT = aio-libs
COMMIT = v0.6.0-0-g39d734a1613adf0dba1cf54886f394cd728c62e6
DIST_VERSION = 0.6.0
RELEASE = 1.aiven.fc$(FEDORA)

all: rpm

build-dep:

clean:
	$(RM) -r rpm/ "$(PROJ)"/

$(PROJ):
	git clone "git@github.com:$(GITHUB_ACCOUNT)/$(PROJ).git"

rpm: $(PROJ)
	$(RM) -r "$@" "$(PROJ)/dist"
	(cd "$(PROJ)" && git fetch && git checkout -f "$(COMMIT)")
	sed -e "s/name=.$(PROJ)./name='python3-$(PYNAME)'/" \
	    -e "s/version=.*/version=\"$(DIST_VERSION)+$(shell git -C $(PROJ) rev-list --count origin/master).$(shell git -C $(PROJ) rev-parse HEAD)\",/" \
	    -i "$(PROJ)/setup.py"
	(cd "$(PROJ)" && python3 setup.py bdist_rpm --release "$(RELEASE)")
	mkdir "$@"/
	cp -a "$(PROJ)/dist"/*.rpm rpm/
	find rpm/ -type f
