export interface LoginIn {
  username: string
  password: string
}

export interface RegisterIn {
  username: string
  password: string
  email?: string
  nickname?: string
  captcha_id?: string
  captcha_answer?: string
}

export interface UserOut {
  id: number
  username: string
  nickname?: string
  email?: string
  role: string
  status: number
  last_login?: string
  create_time?: string
}

export interface LoginOut {
  access_token: string
  token_type: string
  expires_in: number
  user: UserOut
}

export interface ChangePasswordIn {
  old_password: string
  new_password: string
}
