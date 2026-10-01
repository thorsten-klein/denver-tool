# Installed into ~/.config/fish/conf.d/ by refresh-env.sh.
#
# /tmp/denver.env is bash syntax ('export KEY='...'"$KEY"', 'case $- in'),
# which fish can't source. Instead, bash evaluates it and hands back each
# variable it exports as NUL-separated name/value pairs, and fish sets them
# itself -- so this works in any fish (also VS Code's 'fish -ilc env'
# environment resolution), without replacing the shell by another process.
if status is-interactive; and test -r /tmp/denver.env
    # the file's banner (logo) only prints in an interactive shell ('case $- in *i*'),
    # which this bash isn't: on a terminal, let that case match anyway
    set -l banner
    isatty stderr; and set banner 1
    set -l pairs (BANNER=$banner bash --norc --noprofile -c '
        env=$(cat /tmp/denver.env)
        [ -n "$BANNER" ] && env=${env//\'case $- in\'/case i in}
        eval "$env"
        for name in $(sed -n "s/^export \([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p" /tmp/denver.env); do
            printf "%s\0%s\0" "$name" "${!name}"
        done' | string split0)
    for i in (seq 1 2 (count $pairs))
        set -l name $pairs[$i]
        set -l value $pairs[(math $i + 1)]
        # read-only in fish, managed by fish itself
        contains -- $name SHLVL PWD; and continue
        # fish keeps *PATH variables as lists and joins them with ':' on export
        if string match -q -- '*PATH' $name
            set -gx $name (string split -- : $value)
        else
            set -gx $name $value
        end
    end

    # fish reads SHELL_PROMPT_PREFIX natively only from 4.8 on: prepend it here for older ones
    set -l v (string split . -- $version)
    if set -q SHELL_PROMPT_PREFIX; and test "$v[1]" -lt 4 -o \( "$v[1]" -eq 4 -a "$v[2]" -lt 8 \)
        and functions -q fish_prompt; and not functions -q __denver_fish_prompt
        functions -c fish_prompt __denver_fish_prompt
        function fish_prompt
            printf '%s' $SHELL_PROMPT_PREFIX
            __denver_fish_prompt
        end
    end
end
