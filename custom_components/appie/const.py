"""Constants for the Albert Heijn boodschappenlijst integration."""

DOMAIN = "appie"

# Options --------------------------------------------------------------------

# Background polling interval in minutes. 0 = no background polling at all:
# the list is then only fetched when someone opens it in Home Assistant, and
# after changes made from Home Assistant.
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60  # minutes
MAX_SCAN_INTERVAL = 24 * 60  # minutes

# When on, text typed into the to-do list is matched against the AH
# assortment and linked to a product if there is a confident match (falls
# back to free text otherwise). Off = always free text (the reliable default).
CONF_LINK_PRODUCTS = "link_products"
DEFAULT_LINK_PRODUCTS = False

# Demand-driven refresh ---------------------------------------------------------

# When a to-do card/view subscribes to the list, fetch it again unless the
# data is younger than this.
OPEN_MAX_AGE = 30  # seconds
# While at least one view has the list open, refresh it this often so that
# changes made in the AH app (e.g. ticking items off in the store) show up.
OPEN_REFRESH_INTERVAL = 60  # seconds

# Input limits ----------------------------------------------------------------

MAX_QUANTITY = 99
MAX_NAME_LENGTH = 200
MAX_SEARCH_RESULTS = 25

# Services --------------------------------------------------------------------

SERVICE_ADD_ITEM = "add_item"
SERVICE_SEARCH_PRODUCTS = "search_products"
SERVICE_CLEAR_LIST = "clear_list"

ATTR_NAME = "name"
ATTR_QUANTITY = "quantity"
ATTR_PRODUCT_ID = "product_id"
ATTR_LINK_PRODUCT = "link_product"
ATTR_QUERY = "query"
ATTR_LIMIT = "limit"
