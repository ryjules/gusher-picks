import sys
t = open(sys.argv[1]).read(); d = open(sys.argv[2]).read().replace('</', '<\\/')
open(sys.argv[3], 'w').write(t.replace('__DATA__', d))
