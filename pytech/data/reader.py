"""
Act as a wrapper around pandas_datareader and write the responses to the
database to be accessed later.
"""
import datetime as dt
import logging
from typing import Dict, Iterable, Union, Tuple

import numpy as np
import pandas as pd
import pandas_datareader as pdr
from arctic.date import DateRange
from arctic.exceptions import NoDataFoundException
from pandas.tseries.offsets import BDay
from pandas_datareader._utils import RemoteDataError

import pytech.utils.dt_utils as dt_utils
import pytech.utils.pandas_utils as pd_utils
from pytech.decorators.decorators import write_chunks
from pytech.mongo import ARCTIC_STORE
from pytech.mongo.barstore import BarStore
from pytech.utils.exceptions import DataAccessError
from pytech.data._holders import DfLibName

logger = logging.getLogger(__name__)

ticker_input = Union[Iterable, str, pd.DataFrame]
range_type = Union[pd.DatetimeIndex, DateRange]


YAHOO = 'yahoo'
GOOGLE = 'google'
FRED = 'fred'
FAMA_FRENCH = 'famafrench'


class BarReader(object):
    """Read and write data from the DB and the web."""

    def __init__(self, lib_name: str):
        self.lib_name = lib_name

        if lib_name not in ARCTIC_STORE.list_libraries():
            # create the lib if it does not already exist
            ARCTIC_STORE.initialize_library(lib_name,
                                            BarStore.LIBRARY_TYPE)

        self.lib = ARCTIC_STORE[self.lib_name]

    def get_data(self,
                 tickers: ticker_input,
                 source: str = GOOGLE,
                 start: dt.datetime = None,
                 end: dt.datetime = None,
                 check_db: bool = True,
                 filter_data: bool = True,
                 **kwargs) -> Union[pd.DataFrame, Dict[str, pd.DataFrame]]:
        """
        Get data and create a :class:`pd.DataFrame` from it.

        :param tickers: The ticker(s) that data will be retrieved for.
        :param source: The data source.  Options:

            * yahoo
            * google
            * fred
            * famafrench
            * db
            * anything else pandas_datareader supports

        :param start: Left boundary for range.
            defaults to 1/1/2010.
        :param end: Right boundary for range.
            defaults to today.
        :param check_db: Check the database first before making network call.
        :param filter_data: Filter data from the DB. Only used if `check_db` is
            `True`.
        :param kwargs: kwargs are passed blindly to `pandas_datareader`
        :return: A `dict[ticker, DataFrame]`.
        """
        start, end = dt_utils.sanitize_dates(start, end)

        if isinstance(tickers, str):
            try:
                df_lib_name = self._single_get_data(tickers, source, start,
                                                    end, check_db, filter_data,
                                                    **kwargs)
                return df_lib_name.df
            except DataAccessError as e:
                raise DataAccessError(
                        f'Could not get data for ticker: {tickers}') from e
        else:
            if isinstance(tickers, pd.DataFrame):
                tickers = tickers.index
            try:
                return self._mult_tickers_get_data(tickers, source, start, end,
                                                   check_db, filter_data,
                                                   **kwargs)
            except DataAccessError as e:
                raise e

    def _mult_tickers_get_data(self,
                               tickers: Iterable,
                               source: str,
                               start: dt.datetime,
                               end: dt.datetime,
                               check_db: bool,
                               filter_data: bool,
                               **kwargs) -> Dict[str, pd.DataFrame]:
        """Download data for multiple tickers."""
        stocks = {}
        failed = []
        passed = []

        for t in tickers:
            try:
                df_lib_name = self._single_get_data(t, source, start, end,
                                                  check_db, filter_data,
                                                  **kwargs)
                stocks[t] = df_lib_name.df
                passed.append(t)
            except DataAccessError:
                failed.append(t)

        if len(passed) == 0:
            raise DataAccessError('No data could be retrieved.')

        if len(stocks) > 0 and len(failed) > 0 and len(passed) > 0:
            df_na = stocks[passed[0]].copy()
            df_na[:] = np.nan
            for t in failed:
                logger.warning(f'No data could be retrieved for ticker: {t}, '
                               f'replacing with NaN.')
                stocks[t] = df_na

        return stocks

    def _single_get_data(self,
                         ticker: str,
                         source: str,
                         start: dt.datetime,
                         end: dt.datetime,
                         check_db: bool,
                         filter_data: bool,
                         **kwargs):
        """Do the get data method for a single ticker."""
        if check_db:
            try:
                return self._from_db(ticker, source, start, end,
                                     filter_data, **kwargs)
            except DataAccessError:
                # don't raise, try to make the network call
                logger.info(f'Ticker: {ticker} not found in DB.')

        try:
            return self._from_web(ticker, source, start, end, **kwargs)
        except DataAccessError:
            logger.warning(f'Error getting data from {source} '
                           f'for ticker: {ticker}')
            raise

    @write_chunks()
    def _from_web(self,
                  ticker: str,
                  source: str,
                  start: dt.datetime,
                  end: dt.datetime,
                  **kwargs) -> DfLibName:
        """Retrieve data from a web source"""
        _ = kwargs.pop('columns', None)

        try:
            logger.info(f'Making call to {source}. Start date: {start},'
                        f'End date: {end}')
            df = pdr.DataReader(ticker, data_source=source, start=start,
                                end=end, **kwargs)
            if df.empty:
                logger.warning('df retrieved was empty.')
                # the string should be ignored anyway
                return DfLibName(df, lib_name=self.lib_name)
        except RemoteDataError as e:
            logger.warning(f'Error occurred getting data from {source}')
            raise DataAccessError from e
        else:
            df = pd_utils.rename_bar_cols(df)
            df[pd_utils.TICKER_COL] = ticker

            if source == YAHOO:
                # yahoo doesn't set the index :(
                df = df.set_index([pd_utils.DATE_COL])
            else:
                df.index.name = pd_utils.DATE_COL

            return DfLibName(df, lib_name=self.lib_name)

    def _from_db(self,
                 ticker: str,
                 source: str,
                 start: dt.datetime,
                 end: dt.datetime,
                 filter_data: bool = True,
                 **kwargs) -> DfLibName:
        """
        Try to read data from the DB.

        :param ticker: The ticker to retrieve from the DB.
        :param source: Only used if there there is not enough data in the DB.
        :param start: The start of the range.
        :param end: The end of the range.
        :param filter_data: Passed to the read method.
        :param kwargs: Passed to the read method.
        :return: The data frame.
        :raises: NoDataFoundException if no data is found for the given ticker.
        """
        chunk_range = DateRange(start=start, end=end)

        try:
            logger.info(f'Checking DB for ticker: {ticker}')
            df = self.lib.read(ticker, chunk_range=chunk_range,
                               filter_data=filter_data, **kwargs)
        except NoDataFoundException as e:
            raise DataAccessError(f'No data in DB for ticker: {ticker}') from e
        except KeyError as e:
            # TODO: open a bug report against arctic...
            logger.warning('KeyError thrown by Arctic...', e)
            raise DataAccessError(
                    f'Error reading DB for ticker: {ticker}') from e

        logger.debug(f'Found ticker: {ticker} in DB.')

        db_start = dt_utils.parse_date(df.index.min(axis=1))
        db_end = dt_utils.parse_date(df.index.max(axis=1))

        # check that all the requested data is present
        # TODO: deal with days that it is expected that data shouldn't exist.
        if db_start > start and dt_utils.is_trade_day(start):
            # db has less data than requested
            lower_df_lib_name = self._from_web(ticker, source, start,
                                               db_start - BDay())
            lower_df = lower_df_lib_name.df
        else:
            lower_df = None

        if db_end.date() < end.date() and dt_utils.is_trade_day(end):
            # db doesn't have as much data than requested
            upper_df_lib_name = self._from_web(ticker, source, db_end, end)
            upper_df = upper_df_lib_name.df
        else:
            upper_df = None

        new_df = _concat_dfs(lower_df, upper_df, df)
        return DfLibName(new_df, self.lib_name)

    def get_symbols(self):
        for s in self.lib.list_symbols():
            yield s


def _concat_dfs(lower_df: pd.DataFrame,
                upper_df: pd.DataFrame,
                df: pd.DataFrame) -> pd.DataFrame:
    """
    Helper method to concat the missing data frames, where `df` is the original
    df.
    """
    if lower_df is None and upper_df is None:
        # everything is already in the df
        return df
    elif lower_df is not None and upper_df is None:
        # missing only lower data
        return pd.DataFrame(pd.concat([df, lower_df]))
    elif lower_df is None and upper_df is not None:
        # missing only upper data
        return pd.DataFrame(pd.concat([df, upper_df]))
    elif lower_df is not None and upper_df is not None:
        # both missing
        return pd.DataFrame(pd.concat([df, upper_df, lower_df]))
    else:
        return df


def load_from_csv(path: str,
                  start: dt.datetime = None,
                  end: dt.datetime = None) -> None:
    """
    Load a list of tickers from a CSV, and download the data for the
    requested period.

    :param path: The path to the CSV file.
    :param start: The start date to use for the data download.
    :param end: The end date to use for the data download.
    """
