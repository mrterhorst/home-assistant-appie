"""Constants for the Albert Heijn boodschappenlijst integration."""

DOMAIN = "appie"

# Background polling interval in minutes (options flow). 0 = no background
# polling at all: the list is then only fetched when someone opens it in
# Home Assistant, and after changes made from Home Assistant.
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60  # minutes
MAX_SCAN_INTERVAL = 24 * 60  # minutes

# On-demand refresh: when a to-do card/view subscribes to the list, fetch it
# again unless the data is younger than this.
OPEN_MAX_AGE = 30  # seconds
# While at least one view has the list open, refresh it this often so that
# changes made in the AH app (e.g. ticking items off in the store) show up.
OPEN_REFRESH_INTERVAL = 60  # seconds

# Token/session fields stored in the config entry.
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_MEMBER_ID = "member_id"
CONF_EXPIRES_AT = "expires_at"

SERVICE_ADD_ITEM = "add_item"
SERVICE_CHECKOUT = "checkout"
SERVICE_CLEAR_LIST = "clear_list"

ATTR_NAME = "name"
ATTR_QUANTITY = "quantity"
ATTR_FREE_TEXT = "free_text"
ATTR_PRODUCT_ID = "product_id"
