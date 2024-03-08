__author__ = "Jan Liebenau"
import flask
import secrets
from flask import request, render_template
from flask_wtf.csrf import CSRFProtect
from flask_minify import minify
import json
import threading
import logging
import logging.config
import redis
from pyhamtools import LookupLib, Callinfo
import country_converter as coco
from lib.dxtelnet import who
from lib.adxo import get_adxo_events
from lib.qry import query_manager
from lib.cty import prefix_table
from lib.plot_data_provider import ContinentsBandsProvider, SpotsPerMounthProvider, SpotsTrend, HourBand, WorldDxSpotsLive

logging.config.fileConfig("cfg/webapp_log_config.ini", disable_existing_loggers=True)
logger = logging.getLogger(__name__)
logger.info("Start")

app = flask.Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(16)
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=False,
    SESSION_COOKIE_SAMESITE="Strict",
)

inline_script_nonce = ""

csrf = CSRFProtect(app)

logger.debug(app.config)

if app.config["DEBUG"]:
    minify(app=app, html=False, js=False, cssless=False)
else:
    minify(app=app, html=True, js=True, cssless=False)

# load config file
with open("cfg/config.json") as json_data_file:
    cfg = json.load(json_data_file)

logging.debug("CFG:")
logging.debug(cfg)
# load bands file
with open("cfg/bands.json") as json_bands:
    band_frequencies = json.load(json_bands)

# load mode file
with open("cfg/modes.json") as json_modes:
    modes_frequencies = json.load(json_modes)

# load continents-cq file
with open("cfg/continents.json") as json_continents:
    continents_cq = json.load(json_continents)

# read and set default for enabling cq filter
if cfg.get("enable_cq_filter"):
    enable_cq_filter = cfg["enable_cq_filter"].upper()
else:
    enable_cq_filter = "N"

# define country table for search info on callsigns
pfxt = prefix_table()

# setting up pyhamtools to look in ClubLog XML and caching in redis
r = redis.Redis()
lookuplib_clublog = LookupLib(lookuptype="clublogxml", apikey="b56e6fb39efdb6da44d022199c9b0a152c658dc1")
lookuplib_clublog.copy_data_in_redis(redis_prefix="CL", redis_instance=r)
lookuplib_redis_clublog = LookupLib(lookuptype="redis", redis_instance=r, redis_prefix="CL")
callinfo_redis_clublog = Callinfo(lookuplib_redis_clublog)

# create a country converter object
country_converter = coco.CountryConverter()

# create object query manager
qm = query_manager()

# find id  in json : ie frequency / continent
def find_id_json(json_object, name):
    return [obj for obj in json_object if obj["id"] == name][0]


def query_build_callsign(callsign):

    query_string = ""
    if len(callsign) <= 14:
        query_string = (
            "(SELECT rowid, spotter AS de, freq, spotcall AS dx, comment AS comm, time, spotdxcc from spot WHERE spotter='"
            + callsign
            + "'"
        )
        query_string += " ORDER BY rowid desc limit 10)"
        query_string += " UNION "
        query_string += (
            "(SELECT rowid, spotter AS de, freq, spotcall AS dx, comment AS comm, time, spotdxcc from spot WHERE spotcall='"
            + callsign
            + "'"
        )
        query_string += " ORDER BY rowid desc limit 10);"
    else:
        logging.warning("callsign too long")
    return query_string


def query_build(parameters):

    try:
        last_rowid = str(parameters["lr"])  # Last rowid fetched by front end

        get_param = lambda parameters, parm_name: parameters[parm_name] if (parm_name in parameters) else []
        band=get_param(parameters, "band")        
        dere=get_param(parameters, "de_re")  
        dxre=get_param(parameters, "dx_re")  
        mode=get_param(parameters, "mode")
        
        decq = []
        if "cqdeInput" in parameters:
            decq[0] = parameters["cqdeInput"] 

        dxcq = []
        if "cqdxInput" in parameters:   
            dxcq[0] = parameters["cqdxInput"] 

        query_string = ""

        # construct band query decoding frequencies with json file
        band_qry_string = " AND (("
        for i, item_band in enumerate(band):
            freq = find_id_json(band_frequencies["bands"], item_band)
            if i > 0:
                band_qry_string += ") OR ("

            band_qry_string += (
                "freq BETWEEN " + str(freq["min"]) + " AND " + str(freq["max"])
            )

        band_qry_string += "))"
        # construct mode query
        mode_qry_string = " AND  (("
        for i, item_mode in enumerate(mode):
            single_mode = find_id_json(modes_frequencies["modes"], item_mode)
            if i > 0:
                mode_qry_string += ") OR ("
            for j in range(len(single_mode["freq"])):
                if j > 0:
                    mode_qry_string += ") OR ("
                mode_qry_string += (
                    "freq BETWEEN "
                    + str(single_mode["freq"][j]["min"])
                    + " AND "
                    + str(single_mode["freq"][j]["max"])
                )

        mode_qry_string += "))"
        # construct DE continent region query
        dere_qry_string = " AND spottercq IN ("
        for i, item_dere in enumerate(dere):
            continent = find_id_json(continents_cq["continents"], item_dere)
            if i > 0:
                dere_qry_string += ","
            dere_qry_string += str(continent["cq"])
        dere_qry_string += ")"

        # construct DX continent region query
        dxre_qry_string = " AND spotcq IN ("
        for i, item_dxre in enumerate(dxre):
            continent = find_id_json(continents_cq["continents"], item_dxre)
            if i > 0:
                dxre_qry_string += ","
            dxre_qry_string += str(continent["cq"])
        dxre_qry_string += ")"

        if enable_cq_filter == "Y":
            # construct de cq query
            decq_qry_string = ""
            if len(decq) == 1:
                if decq[0].isnumeric():
                    decq_qry_string = " AND spottercq =" + decq[0]
            # construct dx cq query
            dxcq_qry_string = ""
            if len(dxcq) == 1:
                if dxcq[0].isnumeric():
                    dxcq_qry_string = " AND spotcq =" + dxcq[0]

        if last_rowid is None:
            last_rowid = "0"
        if not last_rowid.isnumeric():
            last_rowid = 0

        query_string = (
            #"SELECT rowid, spotter AS de, freq, spotcall AS dx, comment AS comm, time, spotdxcc from spot WHERE rowid > "
            '''SELECT rowid, spotter AS de, freq, spotcall AS dx, time, spotdxcc, spot_additional.program, spot_additional.source, spot_additional.reference, spot_additional.clean_comment as comm
               FROM spot INNER JOIN spot_additional ON spot.rowid = spot_additional.spot_id 
               WHERE rowid > '''
            + last_rowid
        )


        if len(band) > 0:
            query_string += band_qry_string

        if len(mode) > 0:
            query_string += mode_qry_string

        if len(dere) > 0:
            query_string += dere_qry_string

        if len(dxre) > 0:
            query_string += dxre_qry_string

        if enable_cq_filter == "Y":
            if len(decq_qry_string) > 0:
                query_string += decq_qry_string

            if len(dxcq_qry_string) > 0:
                query_string += dxcq_qry_string

        query_string += " ORDER BY rowid desc limit 50;"

    except Exception as e:
        logger.error(e)
        query_string = ""

    return query_string


# the main query to show spots
# it gets url parameter in order to apply the build the right query
# and apply the filter required. It returns a json with the spots
def spotquery(parameters):
    try:

        if 'callsign' in parameters:
            logging.debug('search callsign')
            query_string = query_build_callsign( parameters['callsign'] )
        else:
            logging.debug('search eith other filters')
            query_string = query_build(parameters)

        logger.debug("Execute query: " + query_string)                                              
        qm.qry(query_string)
        data = qm.get_data()
        row_headers = qm.get_headers()

        logger.debug("query done")
        logger.debug("Row headers:")
        logger.debug(row_headers)
        logger.debug("Data:")                            
        logger.debug(data)

        if data is None or len(data) == 0:
            logger.warning("no data found")

        payload = []
        for result in data:
            # create dictionary from recorset
            main_result = dict(zip(row_headers, result))
            
            # Skip to next spot if callsign is not valid
            if not callinfo_redis_clublog.is_valid_callsign(main_result["dx"]):
                continue
            
            # Country name and ISO2 identifier
            country_name = ""
            country_name_short = ""
            country_iso = ""
            
            # find country in ClubLog XML
            country_name = callinfo_redis_clublog.get_country_name(main_result["dx"])
            logging.debug('Country Name = ' + country_name)
            country_name_short = country_converter.convert(country_name, to='name_short')
            logging.debug('Short Country Name = ' + country_name_short)
            
            if country_name_short == 'not found':
                # Country converter cannot find a correspondig country 
                # so it might be a DXCC which is not a country
                
                # Handling some special cases
                if country_name == "ASCENSION ISLAND":
                    country_name_short = "Ascension Island"
                    country_iso = "sh-ac"
                elif country_name == "CANARY ISLANDS":
                    country_name_short = "Canary Islands"
                    country_iso = "ic"
                elif country_name == "CLIPPERTON ISLAND":
                    country_name_short = "Clipperton Island"
                    country_iso = "cp"
                elif country_name == "ENGLAND":
                    country_name_short = "England"
                    country_iso = "gb-eng"
                elif country_name == "REPUBLIC OF KOSOVO":
                    country_name_short = "Kosovo"
                    country_iso = "xk"
                elif country_name == "NORTHERN IRELAND":
                    country_name_short = "Northern Ireland"
                    country_iso = "gb-nir"
                elif country_name == "SAINT HELENA":
                    country_name_short = "St. Helena"
                    country_iso = "sh-hl"
                elif country_name == "SCOTLAND":
                    country_name_short = "Scotland"
                    country_iso = "gb-sct"
                elif country_name == "TRISTAN DA CUNHA & GOUGH ISLANDS":
                    country_name_short = "Tristan da Cunha"
                    country_iso = "sh-ta"
                elif country_name == "UNITED NATIONS HQ":
                    country_name_short = "United Nations"
                    country_iso = "un"
                elif country_name == "WALES":
                    country_name_short = "Wales"
                    country_iso = "gb-wls"
                else:
                    # Still not found. Look it up in Country Files
                    # and get ISO from country.json
                    search_prefix = pfxt.find(main_result["dx"])  
                    country_name_short = search_prefix["country"] 
                    country_iso = search_prefix["iso"]
            else:
                country_iso = country_converter.convert(country_name, to='ISO2').lower()
                logging.debug('Country ISO = ' + country_iso)
                
            main_result["country"] = country_name_short
            main_result["iso"] = country_iso
            
            query_string_references = "SELECT reference_type, reference_name FROM spot_reference WHERE spot_id = " + str(main_result["rowid"])
            logger.debug("Execute query: " + query_string_references)  
            qm.qry(query_string_references)
            data_references = qm.get_data()
            logger.debug("Data:")                            
            logger.debug(data_references)
            main_result["references"] = data_references

            payload.append({**main_result})

        return payload
    except Exception as e:
        logger.error(e)

# find adxo events
adxo_events = None

def get_adxo():
    global adxo_events
    adxo_events = get_adxo_events()
    threading.Timer(12 * 3600, get_adxo).start()


get_adxo()

# create data provider for charts
heatmap_cbp = ContinentsBandsProvider(logger, qm, continents_cq, band_frequencies)
bar_graph_spm = SpotsPerMounthProvider(logger, qm)
line_graph_st = SpotsTrend(logger, qm)
bubble_graph_hb = HourBand(logger, qm, band_frequencies)
geo_graph_wdsl = WorldDxSpotsLive(logger, qm, pfxt)

# ROUTINGS
@app.route("/spotlist", methods=["POST"])
@csrf.exempt
def spotlist():
    logger.debug(request.json)
    response = flask.Response(json.dumps(spotquery(request.json)))
    return response


def who_is_connected():
    host=cfg["telnet"]["host"]
    port=cfg["telnet"]["port"]
    user=cfg["telnet"]["user"]
    password=cfg["telnet"]["password"]
    response = who(host, port, user, password)
    logger.debug("list of connected clusters:")
    logger.debug(response)
    return response

#Calculate nonce token used in inline script and in csp "script-src" header
def get_nonce():
    global inline_script_nonce
    inline_script_nonce = secrets.token_hex()
    return inline_script_nonce

@app.route("/", methods=["GET"])
@app.route("/index.html", methods=["GET"])
def spots():
    response = flask.Response(
        render_template(
            "index.html",
            inline_script_nonce=get_nonce(),
            mycallsign=cfg["mycallsign"],
            telnet=cfg["telnet"]["host"]+":"+cfg["telnet"]["port"],
            mail=cfg["mail"],
            menu_list=cfg["menu"]["menu_list"],
            enable_cq_filter=enable_cq_filter,
            timer_interval=cfg["timer"]["interval"],
            adxo_events=adxo_events,
            continents=continents_cq,
            bands=band_frequencies,
        )
    )
    return response


@app.route("/service-worker.js", methods=["GET"])
def sw():
    return app.send_static_file("pwa/service-worker.js")

@app.route("/offline.html")
def root():
    return app.send_static_file("html/offline.html")

@app.route("/world.json")
def world_data():
    return app.send_static_file("data/world.json")

@app.route("/plots.html")
def plots():
    whoj = who_is_connected()
    response = flask.Response(
        render_template(
            "plots.html",
            inline_script_nonce=get_nonce(),          
            mycallsign=cfg["mycallsign"],
            telnet=cfg["telnet"]["host"]+":"+cfg["telnet"]["port"],
            mail=cfg["mail"],
            menu_list=cfg["menu"]["menu_list"],
            who=whoj,
            continents=continents_cq,
            bands=band_frequencies,
        )
    )
    return response

@app.route("/cookies.html", methods=["GET"])
def cookies():
    response = flask.Response(
        render_template(
            "cookies.html",
            inline_script_nonce=get_nonce(),          
            mycallsign=cfg["mycallsign"],
            telnet=cfg["telnet"]["host"]+":"+cfg["telnet"]["port"],
            mail=cfg["mail"],
            menu_list=cfg["menu"]["menu_list"],
        )
    )
    return response

@app.route("/privacy.html", methods=["GET"])
def privacy():
    response = flask.Response(
        render_template(
            "privacy.html",
            inline_script_nonce=get_nonce(),          
            mycallsign=cfg["mycallsign"],
            telnet=cfg["telnet"]["host"]+":"+cfg["telnet"]["port"],
            mail=cfg["mail"],
            menu_list=cfg["menu"]["menu_list"],
        )
    )
    return response

@app.route("/sitemap.xml")
def sitemap():
    return app.send_static_file("sitemap.xml")


@app.route("/callsign.html", methods=["GET"])
def callsign():
    # payload=spotquery()
    callsign = request.args.get("c")
    response = flask.Response(
        render_template(
            "callsign.html",
            inline_script_nonce=get_nonce(),              
            mycallsign=cfg["mycallsign"],
            telnet=cfg["telnet"]["host"]+":"+cfg["telnet"]["port"],
            mail=cfg["mail"],
            menu_list=cfg["menu"]["menu_list"],
            timer_interval=cfg["timer"]["interval"],
            callsign=callsign,
            adxo_events=adxo_events,
            continents=continents_cq,
            bands=band_frequencies,
        )
    )
    return response


# API that search a callsign and return all informations about that
@app.route("/callsign", methods=["GET"])
def find_callsign():
    callsign = request.args.get("c")
    response = pfxt.find(callsign)
    if response is None:
        response = flask.Response(status=204)
    return response


@app.route("/plot_get_heatmap_data", methods=["POST"])
@csrf.exempt
def get_heatmap_data():
    #continent = request.args.get("continent")
    continent = request.json['continent']
    logger.debug(request.get_json());
    response = flask.Response(json.dumps(heatmap_cbp.get_data(continent)))
    logger.debug(response)
    if response is None:
        response = flask.Response(status=204)
    return response


@app.route("/plot_get_dx_spots_per_month", methods=["POST"])
@csrf.exempt
def get_dx_spots_per_month():
    response = flask.Response(json.dumps(bar_graph_spm.get_data()))
    logger.debug(response)
    if response is None:
        response = flask.Response(status=204)
    return response


@app.route("/plot_get_dx_spots_trend", methods=["POST"])
@csrf.exempt
def get_dx_spots_trend():
    response = flask.Response(json.dumps(line_graph_st.get_data()))
    logger.debug(response)
    if response is None:
        response = flask.Response(status=204)
    return response


@app.route("/plot_get_hour_band", methods=["POST"])
@csrf.exempt
def get_dx_hour_band():
    response = flask.Response(json.dumps(bubble_graph_hb.get_data()))
    logger.debug(response)
    if response is None:
        response = flask.Response(status=204)
    return response


@app.route("/plot_get_world_dx_spots_live", methods=["POST"])
@csrf.exempt
def get_world_dx_spots_live():
    response = flask.Response(json.dumps(geo_graph_wdsl.get_data()))
    logger.debug(response)
    if response is None:
        response = flask.Response(status=204)
    return response


@app.context_processor
def inject_template_scope():
    injections = dict()

    def cookies_check():
        value = request.cookies.get("cookie_consent")
        return value == "true"

    injections.update(cookies_check=cookies_check)
    return injections


@app.after_request
def add_security_headers(resp):

    resp.headers["Strict-Transport-Security"] = "max-age=1000"
    resp.headers["X-Xss-Protection"] = "1; mode=block"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Cache-Control"] = "public, no-cache"
    resp.headers["Pragma"] = "no-cache"    
    
    resp.headers["Content-Security-Policy"] = "\
    default-src 'self';\
    script-src 'self' cdnjs.cloudflare.com cdn.jsdelivr.net 'nonce-"+inline_script_nonce+"';\
    style-src 'self' cdnjs.cloudflare.com cdn.jsdelivr.net 'unsafe-inline';\
    object-src 'none';base-uri 'self';\
    connect-src 'self' cdn.jsdelivr.net cdnjs.cloudflare.com sidc.be www.cqgma.org cqgma.org corsproxy.io;\
    font-src 'self' cdn.jsdelivr.net;\
    frame-src 'self';\
    frame-ancestors 'none';\
    form-action 'none';\
    img-src 'self' data: cdnjs.cloudflare.com sidc.be;\
    manifest-src 'self';\
    media-src 'self';\
    worker-src 'self';\
    "
    return resp
   
    #script-src 'self' cdnjs.cloudflare.com cdn.jsdelivr.net 'nonce-sedfGFG32xs';\
    #script-src 'self' cdnjs.cloudflare.com cdn.jsdelivr.net 'nonce-"+inline_script_nonce+"';\

if __name__ == "__main__":
    app.run(host="0.0.0.0")
