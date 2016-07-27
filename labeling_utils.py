from db_utils import query_hive_ssh, execute_hive_expression, get_hive_timespan
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime
import shutil
import os
import numpy as np
import copy

def get_country_project_condition(d):
        pairs = []
        for c,pl in d.items():
            for p in pl:
                pairs.append("(project = '%s.wikipedia' AND country = '%s')" % (p,c))
        return "(" + " OR ".join(pairs) + ")"


def create_hive_daily_ts(cp_dict, start, stop, table_name):
    
    query = """
        DROP TABLE IF EXISTS censorship.%(table_name)s;
        CREATE TABLE censorship.%(table_name)s
        AS SELECT 
            CONCAT(ts.year,'-',LPAD(ts.month,2,'0'),'-',LPAD(ts.day,2,'0')) as day,
            ts.country, 
            ts.project, 
            ts.page_title,
            ts.n,
            ts.n / agg.n_agg as proportion,
            wd.en_page_title
        FROM 
            (SELECT
                year, 
                month, 
                day, 
                country, 
                project, 
                page_title,
                SUM(view_count) as n
            FROM wmf.pageview_hourly
                WHERE agent_type = 'user'
                AND page_title not RLIKE ':'
                AND %(cp_conditions)s
                AND %(time_conditions)s
            GROUP BY
                year,
                month,
                day,
                country,
                project,
                page_title
            ) ts
        LEFT JOIN
            (SELECT
                year, 
                month, 
                day, 
                project, 
                page_title,
                SUM(view_count) as n_agg
            FROM wmf.pageview_hourly
                WHERE agent_type = 'user'
                AND page_title not RLIKE ':'
                AND %(time_conditions)s
            GROUP BY
                year,
                month,
                day,
                project,
                page_title
            ) agg
            ON (    ts.year = agg.year
                AND ts.month = agg.month
                AND ts.day = agg.day
                AND ts.project = agg.project
                AND ts.page_title = agg.page_title)
        LEFT JOIN censorship.wikidata wd
            ON (ts.page_title = wd.page_title AND ts.project = wd.project);
    """
    params = {'cp_conditions' : get_country_project_condition(cp_dict),
              'time_conditions': get_hive_timespan(start, stop),
              'table_name': table_name
              }
    query %= params
    query_hive_ssh(query, 'ts', priority = True)



def get_all_features(cp_dict, span_comparison, ts_table_name, limit = 100000000, min_views = 100):
    params = copy.deepcopy(span_comparison.params)
    params['cp_conditions'] = get_country_project_condition(cp_dict)
    params['min_views'] =  min_views
    params['ts_table_name'] =  ts_table_name
    params['limit'] = limit

    query = """
    SELECT 
        cmp_table.*,
        proportion_ts,
        ts
    FROM
        (SELECT
            country, 
            project,
            page_title, 
            CONCAT_WS(' ', COLLECT_SET(day_proportion)) as proportion_ts,
            CONCAT_WS(' ', COLLECT_SET(day_n)) as ts
        FROM (
            SELECT
                country, 
                project, 
                page_title,
                CONCAT(day, '|', proportion) as day_proportion,
                CONCAT(day, '|', n) as day_n,
                n
            FROM %(db)s.%(ts_table_name)s
            WHERE %(cp_conditions)s
            ) a
        GROUP BY
            country, 
            project, 
            page_title
        HAVING 
            SUM(n) > %(min_views)d
        LIMIT %(limit)d) ts_table
    JOIN %(db)s.%(span_table)s cmp_table
    ON (
            cmp_table.c = ts_table.country
        AND cmp_table.p = ts_table.project
        AND cmp_table.t = ts_table.page_title
        )
    """
    query %= params

    #print(query)
    #return
    return query_hive_ssh(query, 'ts.tsv', priority = True)





def get_id_conditions(ids, en_title = True):
    condidions = []
    for d in ids:
        if en_title:
            condidions.append("(project = '%(project)s' AND country = '%(country)s' AND en_page_title = '%(en_page_title)s')" % d)
        else:
            condidions.append("(project = '%(project)s' AND country = '%(country)s' AND page_title = '%(page_title)s')" % d)
    return "(" + " OR ".join(condidions) + ")"


     
def get_local_ts(ids, en_titles = True):
    
    params = {
        'id_condition': get_id_conditions(ids, en_title = en_titles),
    }

    query = """
    SELECT *
    FROM censorship.daily_ts2
    WHERE %(id_condition)s
    """

    df =  query_hive_ssh(query % params, 'ts', priority = True)
    df.columns = [c.split('.')[1] for c in df]
    df.index  = pd.to_datetime(df.day)
    return df


def norm_ts(ts):
    ts = (ts - ts.min())
    ts = (ts / ts.max())
    return ts


def get_single_ts(df_ts, start, stop, row, field):
    s = row[field]
    values = []
    index = []
    for elem in s.split(' '):
        i, v = elem.split('|')
        values.append(float(v))
        index.append(i)
    ts = pd.Series(values, index = pd.to_datetime(index))
    ts =  pd.Series(ts, index = pd.date_range(start=start, end=stop, freq='d') )
    ts.fillna(0, inplace = True)

    return ts


def plot_series(df, start, stop, row, smooth = 0):
    ts = get_single_ts(df, start, stop, row, 'ts')
    ts = pd.rolling_mean(ts, smooth)
    ts = norm_ts(ts)

    ts_prop = get_single_ts(df, start, stop, row, 'proportion_ts')
    ts_prop = pd.rolling_mean(ts_prop, smooth)
    
    f, axarr = plt.subplots(2, sharex=True)
    
    english_end = datetime.strptime('2015-06-12 09:40', "%Y-%m-%d %H:%M") # End transition of English Wikipedia, including Mobile

    # plot transition point
    axarr[0].axvline(english_end, color='blue', label = 'HTTPS transition', linewidth=0.5)
    axarr[1].axvline(english_end, color='blue', label = 'HTTPS transition', linewidth=0.5)
    
    axarr[0].plot(ts.index, ts.values)
    axarr[1].plot(ts_prop.index, ts_prop.values)

    return ts, ts_prop, f



def get_id(id_dict):
    return id_dict['en_page_title'] + '_' + id_dict['project'] + '_' + id_dict['country']


def save_series_plot(df, start, stop, id_dict, fig_dir, smooth = 1):
    ts, ts_prop, f = plot_series(df, start, stop, id_dict, smooth = smooth)
    fig_name = get_id(id_dict) + '.pdf'
    fig_name = fig_name.replace('/', '-')
    plt.savefig(os.path.join(fig_dir, fig_name))
    plt.close(f)    

    
def save_all_series_plots(df, start, stop, id_dicts, fig_dir, smooth = 1):
    if os.path.exists(fig_dir):
        shutil.rmtree(fig_dir)
    os.makedirs(fig_dir)
    for d in id_dicts:
        plot_series(df, start, stop, d, fig_dir, smooth = smooth)


def query_span_comparison(span_cmp, country, min_post_article_view = 100, min_wikidata_item_view = 500 ):
    """
    Pull a dataframe with popular articles where pageviews at least doubles
    """
    params = copy.deepcopy(span_cmp.params)
    params['country'] = country
    params['min_post_article_view'] = min_post_article_view
    params['min_wikidata_item_view'] = min_wikidata_item_view


    query = """
    SELECT *
    FROM %(db)s.%(span_table)s
    WHERE post_n_tpc > %(min_post_article_view)d
    AND (pre_n_wd + post_n_wd) > %(min_wikidata_item_view)d
    AND (post_n_tpc / pre_n_tpc) > 2.0
    AND c RLIKE '%(country)s'
    """
    df = query_hive_ssh(query % params , 'get_PVSpanComparison_df', priority = True)
    df.columns = [c.split('.')[1] if len(c.split('.')) == 2 else c for c in df.columns]
    return df


def write_ts_to_file(id_dict, ts, ts_prop, label, f):
    s = '\t'.join(ts.astype(str))
    s += '\t' + '\t'.join(ts_prop.astype(str))
    s += '\t' + '\t'.join([str(v) for v in id_dict.values() if not isinstance(v, str)])
    s += '\t' + label + '\n'
    f.write(s)

def parse(ts):
    times, props = list(zip(*[e.split('|') for e in ts.split(' ')]))
    ts = pd.Series(props, index = pd.to_datetime(times)).astype(float)
    ts = pd.Series(ts, index = pd.date_range(start=start, end=stop, freq='d') )
    ts.fillna(0, inplace = True)
    return ts.values
