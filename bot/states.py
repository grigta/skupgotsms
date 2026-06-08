from aiogram.fsm.state import State, StatesGroup


class BuyFlow(StatesGroup):
    choosing_service = State()
    choosing_plan = State()


class IntervalFlow(StatesGroup):
    waiting_value = State()


class LimitFlow(StatesGroup):
    waiting_value = State()


class LkCookieFlow(StatesGroup):
    waiting_label = State()
    waiting_session = State()
    waiting_xsrf = State()
    waiting_token = State()


class RefundFlow(StatesGroup):
    choosing_service = State()
    waiting_list = State()
    confirming = State()
