#!/bin/sh
# Set up the CouchDB hub of compose.yaml for LiveSync: the system databases, the settings in
# livesync.ini, the LiveSync database and a restricted sync user.
#
#   sh init-couchdb.sh [--env-file FILE] [--database NAME] [--user NAME] [--password-file FILE]
#
# The admin account comes from couchdb.env (COUCHDB_USER, COUCHDB_PASSWORD). The sync
# user is a member of the database only: it cannot list databases, create them or
# change their security. Its password is read from --password-file, or generated into
# it (mode 600) the first time. Passwords are handed to curl through private files,
# never as arguments, and are never printed. Running it again is safe: the database is
# kept and the sync user's password is set to the one in the file.
set -eu

env_file=${COUCHDB_ENV_FILE:-./couchdb.env}
database=obsidian-livesync
sync_user=livesync
password_file=""
url=${COUCHDB_URL:-http://127.0.0.1:5984}

die() {
	printf 'Error: %s\n' "$*" >&2
	exit 1
}

usage() {
	sed -n '2,/^set -eu/{/^#/s/^# \{0,1\}//p;}' "$0"
}

while [ $# -gt 0 ]; do
	case "$1" in
	--env-file | --database | --user | --password-file)
		[ $# -ge 2 ] || die "$1 needs a value"
		case "$1" in
		--env-file) env_file=$2 ;;
		--database) database=$2 ;;
		--user) sync_user=$2 ;;
		--password-file) password_file=$2 ;;
		esac
		shift 2
		;;
	-h | --help)
		usage
		exit 0
		;;
	*) die "unknown option $1 (see --help)" ;;
	esac
done

# The admin password goes to this URL, so it must name this machine in plain http (127.0.0.1,
# localhost or [::1] with a numeric port) or any host over https. Anything after the host must
# start with a slash, and no URL may carry a user name (an "@" would make the real host the
# part after it).
valid_url() {
	case "$1" in
	*@*) return 1 ;;
	http://* | https://*) ;;
	*) return 1 ;;
	esac
	scheme=${1%%://*}
	rest=${1#*://}
	authority=${rest%%/*}
	case "$scheme" in
	http)
		case "$authority" in
		127.0.0.1:* | localhost:* | '[::1]:'*) ;;
		*) return 1 ;;
		esac
		port=${authority##*:}
		case "$port" in "" | *[!0-9]*) return 1 ;; esac
		;;
	https)
		host=${authority%%:*}
		case "$host" in "" | *[!A-Za-z0-9.-]*) return 1 ;; esac
		case "$authority" in
		*:*)
			port=${authority#*:}
			case "$port" in "" | *[!0-9]*) return 1 ;; esac
			;;
		esac
		;;
	esac
}
valid_url "$url" || die "COUCHDB_URL must be http://127.0.0.1:PORT, http://localhost:PORT, http://[::1]:PORT or https://HOST[:PORT], without a user name"
url=${url%/}
case "$database" in
[a-z]*) ;;
*) die "the database name must start with a lowercase letter" ;;
esac
case "$database" in
*[!a-z0-9_-]*) die "the database name may contain only lowercase letters, digits, '_' and '-'" ;;
esac
case "$sync_user" in
"" | *[!A-Za-z0-9_.-]*) die "the sync user name may contain only letters, digits, '.', '_' and '-'" ;;
esac
[ -f "$env_file" ] || die "$env_file not found: create it as described in compose.yaml"
[ -n "$password_file" ] || password_file="$(dirname "$env_file")/sync-user.password"

# Any group or other permission means other users on this machine may read the file.
case "$(ls -ld "$env_file" | cut -c5-10)" in
------) ;;
*) printf 'Warning: %s is accessible to other users; run chmod 600 on it\n' "$env_file" >&2 ;;
esac

# Read the two settings without executing the file.
env_value() {
	sed -n "s/^$1=//p" "$env_file" | head -n 1
}
admin_user=$(env_value COUCHDB_USER)
admin_password=$(env_value COUCHDB_PASSWORD)
[ -n "$admin_user" ] && [ -n "$admin_password" ] || die "$env_file must set COUCHDB_USER and COUCHDB_PASSWORD"

# The sync password.
if [ -f "$password_file" ]; then
	sync_password=$(head -n 1 "$password_file")
	[ -n "$sync_password" ] || die "$password_file is empty"
else
	sync_password=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)
	(umask 077 && printf '%s\n' "$sync_password" >"$password_file")
	printf 'Created %s (mode 600) with a new password for %s\n' "$password_file" "$sync_user"
fi

# The admin account goes into curl's config file between double quotes; the sync password
# also into JSON, so it is limited to characters that need no escaping.
case "$admin_user:$admin_password" in
*[\"\\]*) die "the admin name and password in $env_file must not contain double quotes or backslashes" ;;
esac
case "$sync_password" in
*[!A-Za-z0-9._~+=@-]*) die "the sync password may contain only letters, digits and . _ ~ + = @ -" ;;
esac

work=$(mktemp -d "${TMPDIR:-/tmp}/livesync-init.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM
chmod 700 "$work"
(umask 077 && printf 'user = "%s:%s"\n' "$admin_user" "$admin_password" >"$work/admin.curlrc")
(umask 077 && printf 'user = "%s:%s"\n' "$sync_user" "$sync_password" >"$work/sync.curlrc")

# request CURLRC METHOD PATH [BODYFILE]: prints the HTTP status; the answer is in $work/response.
request() {
	rc=$1
	method=$2
	path=$3
	if [ $# -ge 4 ]; then
		curl -sS --connect-timeout 5 --max-time 60 --config "$work/$rc.curlrc" -o "$work/response" -w '%{http_code}' \
			-X "$method" -H 'Content-Type: application/json' --data-binary "@$4" "$url$path"
	else
		curl -sS --connect-timeout 5 --max-time 60 --config "$work/$rc.curlrc" -o "$work/response" -w '%{http_code}' \
			-X "$method" "$url$path"
	fi
}

printf 'Waiting for CouchDB at %s\n' "$url"
tries=0
while :; do
	code=$(curl -s --connect-timeout 2 -o /dev/null -w '%{http_code}' "$url/_up" 2>/dev/null) || code=000
	case "$code" in 200 | 401) break ;; esac
	tries=$((tries + 1))
	[ "$tries" -lt 30 ] || die "CouchDB does not answer at $url (is the container up? docker compose ps)"
	sleep 2
done

status=$(request admin GET /_all_dbs) || die "could not reach CouchDB"
[ "$status" = 200 ] || die "CouchDB refused the admin account from $env_file (HTTP $status)"

# The system databases a single CouchDB node needs (412: they exist).
for system_database in _users _replicator; do
	status=$(request admin PUT "/$system_database") || die "could not reach CouchDB"
	case "$status" in 201 | 202 | 412) ;; *) die "could not create $system_database (HTTP $status)" ;; esac
done

# The settings in livesync.ini, applied through the configuration API. Each value is sent as
# a JSON string; CouchDB stores it in the couchdb-config volume.
settings_file="$(dirname "$0")/livesync.ini"
[ -f "$settings_file" ] || die "$settings_file not found"
section=""
applied=0
while IFS= read -r line || [ -n "$line" ]; do
	case "$line" in
	'' | \;* | \#*) continue ;;
	\[*\]) section=${line#[}; section=${section%]}; continue ;;
	esac
	key=$(printf '%s' "${line%%=*}" | sed 's/[[:space:]]*$//')
	value=$(printf '%s' "${line#*=}" | sed 's/^[[:space:]]*//')
	case "$section" in "" | *[!A-Za-z0-9_]*) die "bad section [$section] in $settings_file" ;; esac
	case "$key" in "" | *[!A-Za-z0-9_-]*) die "bad setting name '$key' in $settings_file" ;; esac
	printf '"%s"' "$(printf '%s' "$value" | sed 's/\\/\\\\/g; s/"/\\"/g')" >"$work/setting.json"
	status=$(request admin PUT "/_node/_local/_config/$section/$key" "$work/setting.json") || die "could not reach CouchDB"
	[ "$status" = 200 ] || die "could not set $section/$key (HTTP $status)"
	applied=$((applied + 1))
done <"$settings_file"
printf 'Applied %s settings from %s\n' "$applied" "$settings_file"

# The sync user (a CouchDB user document; an existing one is updated with its current revision).
user_path="/_users/org.couchdb.user:$sync_user"
status=$(request admin GET "$user_path") || die "could not reach CouchDB"
query=""
if [ "$status" = 200 ]; then
	revision=$(sed -n 's/.*"_rev" *: *"\([^"]*\)".*/\1/p' "$work/response" | head -n 1)
	[ -n "$revision" ] || die "could not read the revision of the existing user $sync_user"
	query="?rev=$revision"
fi
(umask 077 && printf '{"name":"%s","password":"%s","roles":[],"type":"user"}\n' "$sync_user" "$sync_password" >"$work/user.json")
status=$(request admin PUT "$user_path$query" "$work/user.json") || die "could not reach CouchDB"
case "$status" in 201 | 202) ;; *) die "could not create the sync user (HTTP $status)" ;; esac

# The database: created once; 412 means it exists.
status=$(request admin PUT "/$database") || die "could not reach CouchDB"
case "$status" in
201 | 202) printf 'Created the database %s\n' "$database" ;;
412) printf 'The database %s already exists\n' "$database" ;;
*) die "could not create the database (HTTP $status)" ;;
esac

# Only the sync user (and server admins) may use it; it is a member, not a database admin.
printf '{"admins":{"names":[],"roles":[]},"members":{"names":["%s"],"roles":[]}}\n' "$sync_user" >"$work/security.json"
status=$(request admin PUT "/$database/_security" "$work/security.json") || die "could not reach CouchDB"
[ "$status" = 200 ] || die "could not restrict the database to $sync_user (HTTP $status)"

# What the sync user can and cannot do.
status=$(request sync GET "/$database") || die "could not reach CouchDB"
[ "$status" = 200 ] || die "the sync user cannot open the database (HTTP $status)"
status=$(request sync GET /_all_dbs) || die "could not reach CouchDB"
if [ "$status" = 200 ]; then
	printf 'Warning: the sync user can list all databases; check the CouchDB settings\n' >&2
fi

printf 'Done. In the LiveSync plugin use:\n'
printf '  URI       the https:// address of this hub (tailscale serve), not http://127.0.0.1\n'
printf '  Username  %s\n' "$sync_user"
printf '  Password  the first line of %s\n' "$password_file"
printf '  Database  %s\n' "$database"
