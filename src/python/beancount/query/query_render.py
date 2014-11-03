"""Rendering of rows.
"""
import collections
import datetime
import itertools
import math
from itertools import zip_longest

from beancount.core.amount import Decimal
from beancount.core.amount import ZERO
from beancount.core import data
from beancount.core import inventory
from beancount.core import position
from beancount.query import query_compile
from beancount.parser import options
from beancount.parser import printer
from beancount.ops import summarize
from beancount.utils import misc_utils
from beancount.utils.misc_utils import box
from beancount.reports import table


class ColumnRenderer:
    """Base class for classes that render and compute formatting and width for all
    values that appear within a column. All the values rendered are assumed to
    be of the same type, or None (empty). The class receives all the values that
    will be rendered to accumulate the dimensions it will need to format them
    later on. It is then responsible to render those values in a way that will
    align nicely in a column, in the rendered output, whereby all the values
    render to the same width.
    """
    # Override, the type of object to be rendered.
    dtype = None

    def update(self, value):
        """Update the rendered with the given value.
        Args:
          value: Any object of the type 'dtype'.
        Returns:
          An integer, the number of lines this will get rendered to.
        """
        raise NotImplementedError

    def prepare(self):
        """Prepare to render all values of a column."""
        # No-op. Override if desired.

    def width(self):
        """Return the computed width of this column.
        Returns:
          An integer, the number of characters wide required for this field.
        """
        raise NotImplementedError

    def format(self, value):
        """Format the value.

        Args:
          value: Any object of the type 'dtype'.
        Returns:
          A string, or a list of strings, the rendered and aligned string(s)
          representations of for the value. A value may render on multiple
          lines, which is why a list may be returned here.
        """
        raise NotImplementedError


class StringRenderer(ColumnRenderer):
    """A renderer for left-aligned strings."""
    dtype = str

    def __init__(self):
        self.maxlen = 0

    def update(self, string):
        if string is not None:
            self.maxlen = max(self.maxlen, len(string))

    def prepare(self):
        self.fmt = '{{:<{}.{}}}'.format(self.maxlen, self.maxlen)

    def width(self):
        return self.maxlen

    def format(self, string):
        return self.fmt.format('' if string is None else string)


class DateTimeRenderer(ColumnRenderer):
    """A renderer for decimal numbers."""
    dtype = datetime.date

    def __init__(self):
        self.empty = ' ' * 10

    def update(self, _):
        pass

    def width(self):
        return 10

    def format(self, dtime):
        return self.empty if dtime is None else dtime.strftime('%Y-%m-%d')


class DecimalRenderer(ColumnRenderer):
    """A renderer for decimal numbers."""
    dtype = Decimal

    def __init__(self):
        self.has_negative = False
        self.max_adjusted = 0
        self.min_exponent = 0
        self.total_width = None

    def update(self, number):
        if number is None:
            return
        ntuple = number.as_tuple()
        if ntuple.sign:
            self.has_negative = True
        self.max_adjusted = max(self.max_adjusted, number.adjusted())
        self.min_exponent = min(self.min_exponent, ntuple.exponent)

    def prepare(self):
        digits_sign = 1 if self.has_negative else 0
        digits_integral = max(self.max_adjusted, 0) + 1
        digits_fractional = -self.min_exponent
        digits_period = 1 if digits_fractional > 0 else 0
        width = digits_sign + digits_integral + digits_period + digits_fractional
        self.total_width = width
        self.fmt = '{{:{sign}{width:d}.{precision:d}f}}'.format(
            sign=' ' if digits_sign > 0 else '',
            width=width,
            precision=digits_fractional)
        self.empty = ' ' * width

    def width(self):
        return self.total_width

    def format(self, number):
        return self.empty if number is None else self.fmt.format(number)


class PositionRenderer(ColumnRenderer):
    """A renderer for positions. Inventories renders as a list of position
    strings. Both the unit numbers and the cost numbers are aligned, if any.
    """
    dtype = position.Position

    def __init__(self):
        super().__init__()
        self.units_rdr = DecimalRenderer()
        self.units_ccylen = 0
        self.cost_rdr = DecimalRenderer()
        self.cost_ccylen = 0

    def update(self, pos):
        if pos is None:
            return
        lot = pos.lot
        cost = lot.cost
        self.units_rdr.update(pos.number)
        self.units_ccylen = max(self.units_ccylen, len(lot.currency))
        if cost:
            self.cost_rdr.update(cost.number)
            self.cost_ccylen = max(self.cost_ccylen, len(cost.currency))

    def prepare(self):
        self.units_rdr.prepare()
        self.cost_rdr.prepare()

        fmt_units = '{{:{0}}} {{:{1}}}'.format(self.units_rdr.width(), self.units_ccylen)
        fmt_cost = '{{{{{{:{0}}} {{:{1}}}}}}}'.format(self.cost_rdr.width(), self.cost_ccylen)

        if self.cost_ccylen == 0:
            self.fmt_cost = None # Will not get used.
            self.fmt_nocost = fmt_units
        else:
            self.fmt_cost = '{} {}'.format(fmt_units, fmt_cost)
            self.fmt_nocost = '{} {}'.format(fmt_units, ' ' * len(fmt_cost.format('', '')))

        self.empty = self.fmt_nocost.format('', '')

    def width(self):
        return len(self.empty)

    def format(self, pos):
        strings = []
        if self.cost_ccylen == 0:
            lot = pos.lot
            strings.append(
                self.fmt_nocost.format(
                    self.units_rdr.format(pos.number),
                    lot.currency))

        else:
            lot = pos.lot
            cost = lot.cost
            if cost:
                strings.append(
                    self.fmt_cost.format(
                        self.units_rdr.format(pos.number),
                        lot.currency,
                        self.cost_rdr.format(cost.number if cost else None),
                        cost.currency if cost else ''))
            else:
                strings.append(
                    self.fmt_nocost.format(
                        self.units_rdr.format(pos.number),
                        lot.currency))

        if len(strings) == 1:
            return strings[0]
        elif len(strings) == 0:
            return self.empty
        else:
            return strings


class InventoryRenderer(PositionRenderer):
    """A renderer for Inventoru instances. Inventories renders as a list of position
    strings. Both the unit numbers and the cost numbers are aligned, if any.
    """
    dtype = inventory.Inventory

    def update(self, inv):
        if inv is None:
            return
        for pos in inv.get_positions():
            super().update(pos)

    def format(self, inv):
        strings = []
        if self.cost_ccylen == 0:
            for position in inv.get_positions():
                lot = position.lot
                strings.append(
                    self.fmt_nocost.format(
                        self.units_rdr.format(position.number),
                        lot.currency))

        else:
            for position in inv.get_positions():
                lot = position.lot
                cost = lot.cost
                if cost:
                    strings.append(
                        self.fmt_cost.format(
                            self.units_rdr.format(position.number),
                            lot.currency,
                            self.cost_rdr.format(cost.number if cost else None),
                            cost.currency if cost else ''))
                else:
                    strings.append(
                        self.fmt_nocost.format(
                            self.units_rdr.format(position.number),
                            lot.currency))

        if len(strings) == 1:
            return strings[0]
        elif len(strings) == 0:
            return self.empty
        else:
            return strings


def get_renderers(result_types, result_rows):
    # Create renderers for each column.
    renderers = [RENDERERS[dtype]() for _, dtype in result_types]

    # Prime and prepare each of the renderers with the date in order to be ready
    # to begin rendering with correct alignment.
    for row in result_rows:
        for value, renderer in zip(row, renderers):
            renderer.update(value)

    for renderer in renderers:
        renderer.prepare()

    return renderers


def render_text(result_types, result_rows, file):
    """Render the result of executing a query in text format.

    Args:
      result_types: A list of items describing the names and data types of the items in
        each column.
      result_rows: A list of ResultRow instances.
      file: A file object to render the results to.
    """
    # Important notes:
    #
    # * Some of the data fields must be rendered on multiple lines. This code
    #   deals with this.
    #
    # * Some of the fields must be split into multiple fields for certain
    #   formats in order to be importable in a spreadsheet in a way that numbers
    #   are usable.

    if result_rows:
        assert len(result_types) == len(result_rows[0])

    # # Get the names of the columns.
    num_cols = len(result_types)
    # col_names = [name for name, _ in result_types]

    renderers = get_renderers(result_types, result_rows)

    # Render all the columns of all the rows to strings.
    str_rows = []
    for row in result_rows:
        # Rendering each row involves rendering all the columns, each of which
        # produces one or more lines for its value, and then aligning those
        # columns together to produce a final list of rendered row. This means
        # that a single result row may result in multiple rendered rows.

        # Render all the columns of a row into either strings or lists of
        # strings. This routine also computes the maximum number of rows that a
        # rendered value will generate.
        exp_row = []
        max_lines = 1
        for value, renderer in zip(row, renderers):
            # Update the column renderer.
            exp_lines = renderer.format(value)
            if isinstance(exp_lines, list):
                max_lines = max(max_lines, len(exp_lines))
            exp_row.append(exp_lines)

        # If all the values were rendered directly to strings, this is row
        # renders on a single line. Just append this one row. This is the common
        # case.
        if max_lines == 1:
            str_rows.append(exp_row)

        # Some of the values rendered to more than one line; we need to render
        # them on separate lines and insert filler.
        else:
            # Make sure all values in the column are wrapped in sequences.
            exp_row = [exp_value if isinstance(exp_value, list) else (exp_value,)
                       for exp_value in exp_row]

            # Create a matrix of the column.
            str_lines = [[] for _ in range(max_lines)]
            for exp_value in exp_row:
                for index, exp_line in zip_longest(range(max_lines), exp_value,
                                                   fillvalue=''):
                    str_lines[index].append(exp_line)
            str_rows.extend(str_lines)

    # Compute a final format string.
    formats = ['{{:{}}}'.format(renderer.width()) for renderer in renderers]
    line_formatter = '| ' + ' | '.join(formats) + ' |\n'
    line_body = '-' + '-+-'.join(('-' * len(fmt.format(''))) for fmt in formats) + '-'
    top_line = ",{}.\n".format(line_body)
    middle_line = "+{}+\n".format(line_body)
    bottom_line = "`{}'\n".format(line_body)

    # Render each string row to a single line.
    file.write(top_line)
    file.write(middle_line)
    for str_row in str_rows:
        line = line_formatter.format(*str_row)
        file.write(line)
    file.write(bottom_line)


# A mapping of data-type -> (render-function, alignment)
RENDERERS = {renderer_cls.dtype: renderer_cls
             for renderer_cls in [
                     StringRenderer,
                     DecimalRenderer,
                     DateTimeRenderer,
                     PositionRenderer,
                     InventoryRenderer,
                 ]}


def render_text__old(result_types, result_rows, file):
    table_ = table.create_table(result_rows)
    table.render_table(table_, file, 'text')


# FIXME: Create a StringSetRenderer

# FIXME: You need to render the header.
#
# FIXME: Check out if it's possible to precompile a format string for execution
# the same way it can be done with a regexp, to render faster.
#
# FIXME: Create some sort of column alignment object to accumulate the state of
# alignment and deal with aligning numbers at the dot, and insert spaces for
# lots.
#
# FIXME: For the precision, create some sort of context object that will provide
# the precision to render any number by, indexed by commodity. This should be
# accumulated during rendering and then used for rendering.
#
# FIXME: Provide an option to split apart the commodity and the cost commodity
# into their own columns. This generic object should be working for text, and
# then could be simply reused by the CSV routines.
#
# FIXME: Add EXPLODE keyword to parser in order to allow the breaking out of the
# various columns of an Inventory or Position. This design is a good balance of
# being explicit and succint at the same time. The term 'explode' explains well
# what is meant to happen.
#
#    SELECT account, EXPLODE sum(change) ...
#
# will result in columns:
#
#     account, change_number, change_currency, change_cost_number, change_cost_currency, change_lot_date, change_lot_label
#
