import sys
import os
import random
import time
import logging
import requests
import http.client, urllib
from lxml import html


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S%z"
)

ENV_VAR_OPTIONAL = ['pushover_user', 'pushover_token', 'ntfy_url_topic', 'cookie_filepath']
ENV_VAR_REQUIRED = ['pjportal_user', 'pjportal_pwd', 'ajax_uid', 'pj_tag', 'hospital', 'term', 'check_frequency_lower_limit', 'check_frequency_upper_limit', 'cookie_default_value']
ENV_VAR_list = ENV_VAR_REQUIRED + ENV_VAR_OPTIONAL
ENV_VAR = {}

def load_env():
    global ENV_VAR
    ENV_VAR = {var_name: os.getenv(var_name) for var_name in ENV_VAR_list}
    # logging.info(f"ENV: {ENV_VAR}")
    if os.path.exists(ENV_VAR['cookie_filepath']):
        with open(ENV_VAR['cookie_filepath'], "r") as file:
            ENV_VAR['cookie_default_value'] = file.read().strip()
            logging.info(f"Used cookie {(ENV_VAR['cookie_default_value'])} from file on path {(ENV_VAR['cookie_filepath'])}")
    elif ENV_VAR['cookie_default_value']:
        logging.info(f"No cookie file found on path {(ENV_VAR['cookie_filepath'])}, use cookie saved in env")
        save_cookie(ENV_VAR['cookie_default_value'])
    missing_vars = [key for key, value in ENV_VAR.items() if key not in ENV_VAR_OPTIONAL and value is None]
    if missing_vars:
        raise ValueError(f"Error: Missing required environment variables: {', '.join(missing_vars)}")
    logging.info("Successfully loaded all required environment variables.")
    return ENV_VAR



def save_cookie(cookie_value):
    with open(ENV_VAR['cookie_filepath'], "w") as file:
        file.write(cookie_value)
    logging.info(f"Saved new cookie: {cookie_value} on filepath: {(ENV_VAR['cookie_filepath'])}")



def get_init_session_cookie(session):
    logging.info("Accessing site...")
    response = session.get(url="https://www.pj-portal.de/")
    init_cookie = session.cookies.get_dict().get("PHPSESSID")
    if init_cookie:
        save_cookie(init_cookie)
    return session



def get_auth_session_cookie(session):
    logging.info("Starting authentication...")
    session.headers.update({
        "Origin": "https://www.pj-portal.de",
        "Referer": "https://www.pj-portal.de/",
    })
    data = {
        "name_form_login": "form_login",
        "USER_NAME": ENV_VAR["pjportal_user"],
        "PASSWORT": ENV_VAR["pjportal_pwd"],
        "form_login_buttons": ""
    }
    url = "https://www.pj-portal.de/"
    response = session.post(url, data=data)
    new_cookie = session.cookies.get_dict().get("PHPSESSID")
    if new_cookie:
        save_cookie(new_cookie)
    logging.info("Authentication successfully completed...")
    return session



def request_open_slots(session, cookie=None):
    logging.info("Grabing data...")
    session.headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://www.pj-portal.de",
        "Referer": "https://www.pj-portal.de/index_uu.php?PAGE_ID=101",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
    })
    data = {"AJAX_ID": ENV_VAR["ajax_uid"], "TAB_ID": "Tab_Merkliste"}
    if cookie: # use preset cookie if available and try authentication 
        logging.info(f"Using preset cookie to {cookie} and not requesting a new one")
        session.cookies.set("PHPSESSID", cookie)
    response = session.post("https://www.pj-portal.de/ajax.php", data=data)
    if response.status_code == 200 and not response.content.decode('utf-8') == '{"HTML":" Antwort kein Handler ","ERRORCLASS":2}':
        logging.info(f"Request was successful ({response.status_code}): received data from merkliste")
        return response
    else:
        logging.warning(f"Request failed with status code {response.status_code}")
        logging.warning(f"Response Content: {response.content}")
        raise Exception



def class_to_list(class_string):
    return [cls for cls in class_string.split() if cls]

def extract_table_from_response(response):

    parsing_result_dict = {}

    jsonobj = response.json()
    htmltable = jsonobj.get("HTML")
    tree = html.fromstring(htmltable)
    main_xpath = f"/html/body/table/tr"

    i = 0
    pj_tag = ""
    for row in tree.xpath(f"{main_xpath}"):
        
        i+=1
        if "merkliste" in row.attrib["class"] and "pj_angebot_fach" in row.attrib["class"]:
            cols = row.xpath('.//td')
            for elem in cols:
                if "pj_angebot_fach_angabe" in elem.attrib["class"]:
                    pj_tag = elem.xpath('.//text()')[0].strip()
                    parsing_result_dict[pj_tag] = {}

        elif "pj_angebot_krankenhaus_fach_info" in row.attrib["class"]:
            cols = row.xpath('.//td')
            tertiar_counter = 0
            term_desc = ["first_term", "second_term", "third_term"]
            for elem in cols:
                if 'class' in elem.attrib:

                    if "pj_angebot_krankenhaus_fach" in elem.attrib["class"]:

                        hospital = elem.xpath('.//p[@class="pj_angebot_bezeichnung_Krankenhaus"]/text()')[0].strip()
                        parsing_result_dict[pj_tag][hospital] = {term_desc[0]: None, term_desc[1]: None, term_desc[2]: None}
     
                    if any(item in class_to_list(elem.attrib["class"]) for item in ["tertial_verfuegbarkeit_beendet", "tertial_angebot_verfuegbarkeit"]):
                        testint = elem.xpath('.//text()')
                        try:
                            slots = elem.xpath('.//text()')[1].strip()
                        except:
                            slots = '0/0'
                        slots = slots or '0/0'
                        if slots == 'Tertial beendet':
                            slots = '0/0'
                        parsing_result_dict[pj_tag][hospital][term_desc[tertiar_counter]] = tuple(map(int, slots.split('/')))
                        tertiar_counter += 1

    return parsing_result_dict


def send_push_message(msg):
    pushover_user = os.environ.get('pushover_user')
    pushover_token = os.environ.get('pushover_token')
    ntfy_url_topic = os.environ.get('ntfy_url_topic')
    if pushover_user and pushover_token:
        send_pushover_notification(msg)
    if ntfy_url_topic:
        send_ntfy_notification(msg)
    if not (pushover_user and pushover_token) and not ntfy_url_topic:
        logging.warning("No credentials for either Pushover or ntfy specified as env.")


def send_pushover_notification(msg):
    conn = http.client.HTTPSConnection("api.pushover.net:443")
    conn.request("POST", "/1/messages.json",
    urllib.parse.urlencode({
        "token": ENV_VAR['pushover_token'],
        "user": ENV_VAR['pushover_user'],
        "message": msg,
    }), { "Content-type": "application/x-www-form-urlencoded" })
    response = conn.getresponse()
    if not response.status == 200:
        logging.warning(f"Notification failed. Status: {response.status}, Reason: {response.reason}")


def send_ntfy_notification(msg):
    url = f'{ENV_VAR["ntfy_url_topic"]}'
    headers = {
        "Title": "Found something on pj-portal.de",
        "Priority": str(5)
    }
    response = requests.post(url, data=msg.encode('utf-8'), headers=headers)
    if not response.status_code == 200:
        logging.warning(f"Failed to send notification through ntfy: {response.status_code} - {response.text}")


def run_main():

    session = requests.Session()
    session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
        })

    def run_auth(session):
        session = get_init_session_cookie(session)
        session = get_auth_session_cookie(session)
        return session

    def run_table_check(table_dict, pj_tag, hospital, term):
        logging.info("Parsing data from request and checking the table...")
        logging.info(table_dict)
        result_tuple = table_dict[pj_tag][hospital][term]
        info = f"{result_tuple[0]}/{result_tuple[1]}"
        if result_tuple[0] > 0:
            msg = f"Found something for {pj_tag}, {hospital}, {term}! {info}!"
            logging.info(msg)
            send_push_message(msg=msg)
        else:
            logging.info(f"Nothing found for {pj_tag}, {hospital}, {term}: {info}")


    try:
        response = request_open_slots(session, cookie=ENV_VAR['cookie_default_value'])
        table_dict = extract_table_from_response(response)
        
        run_table_check(table_dict=table_dict, pj_tag=ENV_VAR["pj_tag"], hospital=ENV_VAR["hospital"], term=ENV_VAR["term"])

    except IndexError as e:
        logging.warnign("Got a IndexError")
        sys.exit(1)

    except Exception as e:
        logging.warning("Trying new authentication!")
        session.cookies.clear()
        session = run_auth(session)
        response = request_open_slots(session)
        table_dict = extract_table_from_response(response)
        run_table_check(table_dict=table_dict, pj_tag=ENV_VAR["pj_tag"], hospital=ENV_VAR["hospital"], term=ENV_VAR["term"])
    
    logging.info("Script completed.")


MAX_RETRIES = 5 

if __name__ == "__main__":
    logging.info("--------------------------------------------")
    logging.info("Script started")
    logging.info("Loading ENV...")
    ENV_VAR = load_env()
    sleeptime = random.randint(int(ENV_VAR['check_frequency_lower_limit']), int(ENV_VAR['check_frequency_upper_limit']))
    logging.info(f"Sleeping for {sleeptime}s...")
    time.sleep(sleeptime)
    retries = 0
    while retries < MAX_RETRIES:
        try:
            run_main()
            if retries > 0:
                logging.info(f"Script executed successfully after {retries} retries.")
            break
        except Exception as e:
            retries += 1
            logging.error(f"Attempt {retries} failed with error: {e}")
            if retries >= MAX_RETRIES:
                error_msg = f"pj-portal.py failed after {MAX_RETRIES} attempts with error: {e}"
                send_push_message(msg=f"Script Failure! Script will be stopped. Following error occurred: {error_msg}")
                sys.exit(1)
            else:
                wait_time = 2 ** retries
                logging.info(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)