CC ?= cc
CFLAGS ?= -O2 -pipe -fPIE -fstack-protector-strong -D_FORTIFY_SOURCE=2 -Wall -Wextra -Werror
LDFLAGS ?= -Wl,-z,relro,-z,now,-z,noexecstack -pie

.PHONY: all clean

all: onebrain-scanner-sandbox

onebrain-scanner-sandbox: onebrain_scanner_sandbox.c
	$(CC) $(CFLAGS) $< -o $@ $(LDFLAGS) -lseccomp

clean:
	rm -f onebrain-scanner-sandbox
