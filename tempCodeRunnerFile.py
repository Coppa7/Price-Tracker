from flask import Flask, render_template, request, session, url_for, redirect, jsonify, g
from datetime import timedelta
from scraper.amazon_scraper import get_product_details
import sqlite3
import os
from datetime import date, datetime as dt
import uuid

app = Flask(__name__)
app.secret_key = "123 stella"
# Change secret key in prod :)

app.permanent_session_lifetime = timedelta(days=3650)
#Cookies can get removed by the user, otherwise they're semi-permanent

#We create a global database (all the bookmarked products shared between the users)
folder = 'database_dir'